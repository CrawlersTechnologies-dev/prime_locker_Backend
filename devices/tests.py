import json
import hmac
import hashlib
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import Role, PlatformSettings
from devices.models import (
    Device,
    DeviceLockStatus,
    LockProvider,
    OSType,
    ApprovedDeviceTAC,
    TACEnvironment,
    InstallationVideo,
    VideoCategory,
    VideoSourceType
)
from devices.services.rocketpay_service import RocketPayService

User = get_user_model()


@override_settings(ROCKETPAY_SIMULATION=True)
class DevicesBackendTests(TestCase):
    def setUp(self):
        self.retailer = User.objects.create_user(
            username='test_retailer_dev',
            email='retailer_dev@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919833333333',
            business_name='Prime Retailer Store'
        )
        self.customer = User.objects.create_user(
            username='test_customer_dev',
            email='customer_dev@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919844444444',
            parent=self.retailer
        )

        self.rocketpay_device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671060000111',
            brand='INFINIX',
            model='X6730',
            os_type=OSType.ANDROID,
            status=DeviceLockStatus.ACTIVE,
            lock_provider=LockProvider.ROCKETPAY,
            provider_device_id='rp_key_12345'
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.retailer)

    def test_webhook_signature_security(self):
        """Tests that webhooks without a valid HMAC signature or without a configured secret are strictly rejected"""
        webhook_url = '/api/integrations/rocketpay/webhook/'
        payload = json.dumps({
            "event": "unlocked",
            "device_id": self.rocketpay_device.provider_device_id,
            "imei": self.rocketpay_device.imei
        }).encode('utf-8')

        ps = PlatformSettings.get_settings()

        # Case 1: No secret configured -> Must reject with 401
        ps.rocketpay_webhook_secret = ''
        ps.save()
        resp_no_secret = self.client.post(
            webhook_url,
            data=payload,
            content_type='application/json',
            HTTP_X_ROCKETPAY_SIGNATURE='any_signature'
        )
        self.assertEqual(resp_no_secret.status_code, status.HTTP_401_UNAUTHORIZED)

        # Case 2: Secret configured, but invalid signature -> Must reject with 401
        secret = "super_secure_webhook_secret_key_123"
        ps.rocketpay_webhook_secret = secret
        ps.save()

        resp_bad_sig = self.client.post(
            webhook_url,
            data=payload,
            content_type='application/json',
            HTTP_X_ROCKETPAY_SIGNATURE='invalid_tampered_signature'
        )
        self.assertEqual(resp_bad_sig.status_code, status.HTTP_401_UNAUTHORIZED)

        # Case 3: Valid HMAC signature -> Must succeed with 200
        valid_sig = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
        resp_valid = self.client.post(
            webhook_url,
            data=payload,
            content_type='application/json',
            HTTP_X_ROCKETPAY_SIGNATURE=valid_sig
        )
        self.assertEqual(resp_valid.status_code, status.HTTP_200_OK)

    def test_app_controls_on_rocketpay_devices(self):
        """
        Tests RP6 & Page 9 bottom note:
        - Running collateral phones on RocketPay strictly refuse app controls and camera blocks (400)
        - Standard DLC phones forward controls to RocketPay (success)
        """
        # Create a running collateral device
        from devices.models import DeviceScannerState
        running_device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671060000999',
            brand='INFINIX',
            model='X6730',
            os_type=OSType.ANDROID,
            device_state=DeviceScannerState.RUNNING,
            status=DeviceLockStatus.ACTIVE,
            lock_provider=LockProvider.ROCKETPAY,
            provider_device_id='rp_key_running_999'
        )

        # 1. Running phone must reject app lock with 400
        resp_run_app = self.client.post(f'/api/devices/{running_device.id}/apps/lock/', {'package_name': 'com.instagram.android'}, format='json')
        self.assertEqual(resp_run_app.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Running collateral phones', resp_run_app.data.get('error', ''))

        # 2. Running phone must reject camera toggle with 400
        resp_run_cam = self.client.post(f'/api/devices/{running_device.id}/toggle-camera/', format='json')
        self.assertEqual(resp_run_cam.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Standard DLC phone forwards app lock successfully (RP6)
        resp_std_app = self.client.post(f'/api/devices/{self.rocketpay_device.id}/apps/lock/', {'package_name': 'com.instagram.android'}, format='json')
        self.assertEqual(resp_std_app.status_code, status.HTTP_200_OK)

        # 4. Standard DLC phone forwards social media toggle successfully (RP6)
        resp_std_soc = self.client.post(f'/api/devices/{self.rocketpay_device.id}/toggle-social-media/', format='json')
        self.assertEqual(resp_std_soc.status_code, status.HTTP_200_OK)

        # 5. RP9: Wallpaper control returns 400 on RocketPay phones
        resp_wall = self.client.post(f'/api/devices/{self.rocketpay_device.id}/set-wallpaper/', {'wallpaper_url': 'https://example.com/bg.png'}, format='json')
        self.assertEqual(resp_wall.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not supported on RocketPay', resp_wall.data.get('error', ''))

    def test_controls_status_endpoint(self):
        """Tests RP8 GET /api/devices/:id/controls-status/ returns RocketPay control states"""
        resp = self.client.get(f'/api/devices/{self.rocketpay_device.id}/controls-status/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('on', resp.data)
        self.assertIn('off', resp.data)

    def test_rocketpay_offline_code_generation(self):
        """Tests RP1 generate-offline-code calls RocketPay FETCH API for RocketPay phones"""
        resp = self.client.post(f'/api/devices/{self.rocketpay_device.id}/generate-offline-code/', format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data.get('offline_unlock_code'))

    @override_settings(ROCKETPAY_SIMULATION=False)
    def test_no_silent_simulation_when_simulation_disabled(self):
        """Tests R2: Without API key and with simulation disabled, RocketPay actions strictly return an error"""
        ps = PlatformSettings.get_settings()
        ps.rocketpay_api_key = ''
        ps.save()
        res = RocketPayService.lock_device(self.rocketpay_device)
        self.assertFalse(res.get('success'))
        self.assertIn('simulation is disabled', res.get('error', ''))

    def test_device_alerts_status_board(self):
        """Tests GET /api/devices/alerts/ surfaces tamper, failed command, and offline devices"""
        # Create Tamper device
        Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671060000222',
            brand='VIVO',
            model='Y35',
            status=DeviceLockStatus.LOCKED,
            provider_status='TAMPERED'
        )

        # Create Failed command device
        Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671060000333',
            brand='VIVO',
            model='Y36',
            status=DeviceLockStatus.ACTIVE,
            provider_status='FAILED',
            provider_error='Device unreachable by OEM'
        )

        resp = self.client.get('/api/devices/alerts/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data

        self.assertIn('summary', data)
        self.assertGreaterEqual(data['summary']['tamper_count'], 1)
        self.assertGreaterEqual(data['summary']['failed_commands_count'], 1)

    def test_send_offline_code_to_retailer(self):
        """Tests POST /api/devices/:id/send-offline-code/ generates code and sends in-app notification"""
        resp = self.client.post(f'/api/devices/{self.rocketpay_device.id}/send-offline-code/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data.get('success'))
        self.assertTrue(resp.data.get('code'))
        self.assertTrue(resp.data.get('in_app_notification_dispatched'))


class InstallationVideoTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_video_user',
            email='admin_video@test.com',
            password='Password@123',
            role=Role.SUPER_ADMIN
        )
        self.retailer = User.objects.create_user(
            username='retailer_video_user',
            email='retailer_video@test.com',
            password='Password@123',
            role=Role.RETAILER
        )
        self.distributor = User.objects.create_user(
            username='distributor_video_user',
            email='distributor_video@test.com',
            password='Password@123',
            role=Role.DISTRIBUTOR
        )

        self.client_admin = APIClient()
        self.client_admin.force_authenticate(user=self.admin)

        self.client_retailer = APIClient()
        self.client_retailer.force_authenticate(user=self.retailer)

    def test_admin_can_create_installation_video_with_url(self):
        """Admin can post an installation video with a YouTube/external URL"""
        payload = {
            "title": "Vivo & Infinix DLC Activation Guide",
            "description": "Step-by-step video guide for activating DLC on Vivo and Infinix phones.",
            "category": "DLC_LOCKER",
            "source_type": "URL",
            "video_url": "https://www.youtube.com/watch?v=dlc_test_guide",
            "duration": "04:30",
            "target_role": "ALL",
            "display_order": 1,
            "is_active": True
        }
        resp = self.client_admin.post('/api/installation-videos/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['title'], payload['title'])
        self.assertEqual(resp.data['category'], "DLC_LOCKER")
        self.assertEqual(resp.data['stream_url'], payload['video_url'])

    def test_retailer_can_view_active_installation_videos(self):
        """Retailer can list active installation videos, but not inactive ones"""
        # Active video
        video_active = InstallationVideo.objects.create(
            title="Android DPC App Setup",
            category=VideoCategory.ANDROID_DPC,
            source_type=VideoSourceType.URL,
            video_url="https://www.youtube.com/watch?v=dpc_setup",
            display_order=1,
            is_active=True,
            created_by=self.admin
        )
        # Inactive video (draft)
        InstallationVideo.objects.create(
            title="Secret Internal Guide",
            category=VideoCategory.GENERAL,
            source_type=VideoSourceType.URL,
            video_url="https://www.youtube.com/watch?v=secret",
            is_active=False,
            created_by=self.admin
        )

        resp = self.client_retailer.get('/api/installation-videos/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        titles = [v['title'] for v in resp.data]
        self.assertIn("Android DPC App Setup", titles)
        self.assertNotIn("Secret Internal Guide", titles)

    def test_retailer_can_filter_videos_by_category(self):
        """Retailer can filter videos by ?category=DLC_LOCKER or ?category=ANDROID_DPC"""
        InstallationVideo.objects.create(
            title="DLC Guide",
            category=VideoCategory.DLC_LOCKER,
            source_type=VideoSourceType.URL,
            video_url="https://test.com/dlc",
            is_active=True
        )
        InstallationVideo.objects.create(
            title="iPhone MDM Guide",
            category=VideoCategory.IPHONE,
            source_type=VideoSourceType.URL,
            video_url="https://test.com/iphone",
            is_active=True
        )

        # Filter DLC
        resp_dlc = self.client_retailer.get('/api/installation-videos/?category=DLC_LOCKER')
        self.assertEqual(resp_dlc.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp_dlc.data), 1)
        self.assertEqual(resp_dlc.data[0]['category'], "DLC_LOCKER")

        # Filter iPhone
        resp_iphone = self.client_retailer.get('/api/installation-videos/?category=IPHONE')
        self.assertEqual(resp_iphone.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp_iphone.data), 1)
        self.assertEqual(resp_iphone.data[0]['category'], "IPHONE")

    def test_retailer_cannot_create_or_delete_videos(self):
        """Retailer is refused write access (HTTP 403 Forbidden)"""
        payload = {
            "title": "Hacked Video",
            "category": "GENERAL",
            "source_type": "URL",
            "video_url": "https://bad.com/video"
        }
        resp = self.client_retailer.post('/api/installation-videos/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # Attempt delete
        video = InstallationVideo.objects.create(
            title="Admin Video",
            category=VideoCategory.GENERAL,
            source_type=VideoSourceType.URL,
            video_url="https://test.com/v",
            is_active=True
        )
        resp_del = self.client_retailer.delete(f'/api/installation-videos/{video.id}/')
        self.assertEqual(resp_del.status_code, status.HTTP_403_FORBIDDEN)



from devices.services.partner_mdm_service import PartnerMDMService
from emi.models import CustomerEMIContract, EMIInstallment, InstallmentStatus, EMIContractStatus, PaymentMode


@override_settings(ROCKETPAY_SIMULATION=True, PARTNER_MDM_SIMULATION=True, CASHFREE_SIMULATION=True)
class PartnerMDMBackendTests(TestCase):
    def setUp(self):
        self.retailer = User.objects.create_user(
            username='test_retailer_pm',
            email='retailer_pm@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919877711111',
            business_name='Prime Mobile Hub'
        )
        self.customer = User.objects.create_user(
            username='test_customer_pm',
            email='customer_pm@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919877722222',
            parent=self.retailer
        )

        self.approved_tac = ApprovedDeviceTAC.objects.create(
            tac='86667107',
            brand='REDMI',
            model_number='Note 13',
            marketing_name='Redmi Note 13',
            environment=TACEnvironment.ALL,
            active=True
        )

        self.pm_device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671070000123',
            brand='REDMI',
            model='Note 13',
            os_type=OSType.ANDROID,
            status=DeviceLockStatus.ACTIVE,
            lock_provider=LockProvider.PARTNER_MDM,
            provider_device_id='mock_pm_dev_123',
            qr_code_url='https://api.qrserver.com/v1/create-qr-code/?data=simulated_qr_code'
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.retailer)

    def test_partner_mdm_service_direct_methods(self):
        """Verify all direct simulation methods in PartnerMDMService"""
        token = PartnerMDMService.get_token()
        self.assertTrue(token.startswith('mock_jwt_token_'))

        # Token caching check
        token2 = PartnerMDMService.get_token()
        self.assertEqual(token, token2)

        # Device enrollment simulation
        enroll_res = PartnerMDMService.enroll_device(
            device=self.pm_device,
            customer_name='Rajesh Kumar',
            customer_mobile='+919877722222',
            loan_data={'monthly_emi': 1500, 'number_of_emi': 6}
        )
        self.assertTrue(enroll_res['success'])
        self.assertIn('qr_code_url', enroll_res)
        self.assertIn('provider_device_id', enroll_res)

        # Policy update (including hide application)
        policy_res = PartnerMDMService.update_policy(
            device=self.pm_device,
            hide_app=True,
            block_social=True,
            disable_camera=True,
            disable_calls=True,
            disable_wifi=True
        )
        self.assertTrue(policy_res['success'])

        # Lock & Unlock
        lock_res = PartnerMDMService.lock_device(self.pm_device)
        self.assertTrue(lock_res['success'])

        unlock_res = PartnerMDMService.unlock_device(self.pm_device)
        self.assertTrue(unlock_res['success'])

        # Audio reminder / Siren
        audio_res = PartnerMDMService.audio_reminder(self.pm_device)
        self.assertTrue(audio_res['success'])

        # Push notification
        notif_res = PartnerMDMService.send_notification(self.pm_device, 'EMI Due', 'Please pay immediately')
        self.assertTrue(notif_res['success'])

        # Reboot
        reboot_res = PartnerMDMService.reboot_device(self.pm_device)
        self.assertTrue(reboot_res['success'])

        # Location
        loc_res = PartnerMDMService.get_location(self.pm_device)
        self.assertTrue(loc_res['success'])
        self.assertIn('latitude', loc_res)

        # Offline code
        code_res = PartnerMDMService.get_offline_code(self.pm_device)
        self.assertTrue(code_res['success'])
        self.assertIn('offline_unlock_code', code_res)

        # Remove / Release device
        remove_res = PartnerMDMService.remove_device(self.pm_device)
        self.assertTrue(remove_res['success'])

    def test_api_enrollment_with_partner_mdm(self):
        """Verify /api/customers/enroll/ with lock_provider=PARTNER_MDM returns qr_code_data"""
        from keys.models import DeviceKey, KeyStatus
        key = DeviceKey.objects.create(
            key_string='AND-TEST-PM-001',
            device_type='ANDROID',
            status=KeyStatus.AVAILABLE,
            current_owner=self.retailer
        )
        payload = {
            'full_name': 'Suresh Patel',
            'mobile_number': '+919877733333',
            'imei': '866671070000456',
            'brand': 'REDMI',
            'model': 'Note 13',
            'activation_key_string': key.key_string,
            'lock_provider': 'PARTNER_MDM',
            'monthly_emi': 2000,
            'number_of_emi': 6
        }
        resp = self.client.post('/api/customers/enroll/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn('device', resp.data)
        device_data = resp.data['device']
        self.assertEqual(device_data['lock_provider'], 'PARTNER_MDM')
        self.assertTrue(device_data.get('qr_code_data'))

        # Verify device in DB
        created_dev = Device.objects.get(imei='866671070000456')
        self.assertEqual(created_dev.lock_provider, LockProvider.PARTNER_MDM)
        self.assertTrue(created_dev.qr_code_url)

    def test_api_device_controls_partner_mdm(self):
        """Verify DeviceViewSet lock, unlock, hide-app, audio reminder, reboot for PARTNER_MDM"""
        # 1. Lock device
        resp_lock = self.client.post(f'/api/devices/{self.pm_device.id}/lock/', format='json')
        self.assertEqual(resp_lock.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.status, DeviceLockStatus.LOCKED)

        # 2. Unlock device
        resp_unlock = self.client.post(f'/api/devices/{self.pm_device.id}/unlock/', format='json')
        self.assertEqual(resp_unlock.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.status, DeviceLockStatus.ACTIVE)

        # 3. Toggle Hide App
        resp_hide = self.client.post(
            f'/api/devices/{self.pm_device.id}/toggle-hide-app/',
            {'hide_application': True},
            format='json'
        )
        self.assertEqual(resp_hide.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertTrue(self.pm_device.hide_application)

        # Toggle Hide App off
        resp_unhide = self.client.post(
            f'/api/devices/{self.pm_device.id}/toggle-hide-app/',
            {'hide_application': False},
            format='json'
        )
        self.assertEqual(resp_unhide.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertFalse(self.pm_device.hide_application)

        # 4. Audio reminder (Siren)
        resp_audio = self.client.post(f'/api/devices/{self.pm_device.id}/audio-reminder/', format='json')
        self.assertEqual(resp_audio.status_code, status.HTTP_200_OK)

        # 5. Reboot device
        resp_reboot = self.client.post(f'/api/devices/{self.pm_device.id}/reboot/', format='json')
        self.assertEqual(resp_reboot.status_code, status.HTTP_200_OK)

        # 6. Refresh location
        resp_loc = self.client.post(f'/api/devices/{self.pm_device.id}/refresh-location/', format='json')
        self.assertEqual(resp_loc.status_code, status.HTTP_200_OK)

    def test_emi_payment_auto_unlock_and_release_partner_mdm(self):
        """Verify in-store payment auto-unlocks PARTNER_MDM device and final payment releases it"""
        contract = CustomerEMIContract.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            lock_device=self.pm_device,
            device_imei=self.pm_device.imei,
            device_name=f"{self.pm_device.brand} {self.pm_device.model}",
            total_amount=Decimal('5000.00'),
            down_payment=Decimal('1000.00'),
            financed_amount=Decimal('4000.00'),
            tenure_months=2,
            monthly_emi_amount=Decimal('2000.00'),
            first_emi_date=timezone.localdate(),
            status=EMIContractStatus.ACTIVE
        )
        inst1 = EMIInstallment.objects.create(
            contract=contract,
            installment_number=1,
            due_date=timezone.localdate() - timedelta(days=2),
            amount=Decimal('2000.00'),
            status=InstallmentStatus.OVERDUE
        )
        inst2 = EMIInstallment.objects.create(
            contract=contract,
            installment_number=2,
            due_date=timezone.localdate() + timedelta(days=28),
            amount=Decimal('2000.00'),
            status=InstallmentStatus.UPCOMING
        )

        # Lock the device first
        self.pm_device.status = DeviceLockStatus.LOCKED
        self.pm_device.save()

        # Pay Installment 1 -> Should auto-unlock device
        with self.captureOnCommitCallbacks(execute=True):
            resp_pay1 = self.client.post(
                f'/api/emi-installments/{inst1.id}/record-payment/',
                {'amount': '2000.00', 'payment_mode': PaymentMode.CASH, 'transaction_ref': 'CASH-PM-001'},
                format='json'
            )
        self.assertEqual(resp_pay1.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.status, DeviceLockStatus.ACTIVE)

        # Pay Installment 2 (Final) -> Contract completed -> Should release device
        with self.captureOnCommitCallbacks(execute=True):
            resp_pay2 = self.client.post(
                f'/api/emi-installments/{inst2.id}/record-payment/',
                {'amount': '2000.00', 'payment_mode': PaymentMode.CASH, 'transaction_ref': 'CASH-PM-002'},
                format='json'
            )
        self.assertEqual(resp_pay2.status_code, status.HTTP_200_OK)
        contract.refresh_from_db()
        self.assertEqual(contract.status, EMIContractStatus.COMPLETED)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.status, DeviceLockStatus.UNENROLLED)
        self.assertEqual(self.pm_device.provider_status, "UNREGISTERED")

    def test_device_activate_action(self):
        from devices.models import DeviceLockStatus
        self.pm_device.status = DeviceLockStatus.INACTIVE
        self.pm_device.is_activated = False
        self.pm_device.save()

        resp = self.client.post(f'/api/devices/{self.pm_device.id}/activate/', {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertTrue(self.pm_device.is_activated)
        self.assertEqual(self.pm_device.status, DeviceLockStatus.ACTIVE)

    def test_device_uninstall_action(self):
        from devices.models import DeviceLockStatus
        self.pm_device.status = DeviceLockStatus.ACTIVE
        self.pm_device.save()

        resp = self.client.post(f'/api/devices/{self.pm_device.id}/uninstall/', {'reason': 'EMI Completed'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.status, DeviceLockStatus.UNENROLLED)
        self.assertEqual(self.pm_device.provider_status, 'UNREGISTERED')
        self.assertEqual(resp.data.get('customer_status'), 'UNINSTALL')

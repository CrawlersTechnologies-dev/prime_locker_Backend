import json
import hmac
import hashlib
from unittest.mock import patch
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

    def test_inactive_device_imei_and_imei2_edit_allowed(self):
        """Tests PATCH /api/devices/:id/ allows imei and imei2 edit when device is inactive"""
        inactive_dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='352099001111001',
            secondary_imei='352099001111002',
            brand='SAMSUNG',
            model='Galaxy A14',
            status=DeviceLockStatus.INACTIVE,
            is_activated=False
        )
        patch_payload = {
            'imei': '352099009999001',
            'imei2': '352099009999002'
        }
        resp = self.client.patch(f'/api/devices/{inactive_dev.id}/', data=patch_payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        inactive_dev.refresh_from_db()
        self.assertEqual(inactive_dev.imei, '352099009999001')
        self.assertEqual(inactive_dev.secondary_imei, '352099009999002')

    def test_active_device_imei_edit_blocked(self):
        """Tests PATCH /api/devices/:id/ blocks imei edit when device is already activated"""
        active_dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='352099002222001',
            brand='SAMSUNG',
            model='Galaxy A14',
            status=DeviceLockStatus.ACTIVE,
            is_activated=True
        )
        resp = self.client.patch(f'/api/devices/{active_dev.id}/', data={'imei': '352099009999003'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('imei', resp.data)
        self.assertIn('cannot be modified once the device has been activated', resp.data['imei'][0])

    def test_live_location_endpoint_and_ping(self):
        """Tests live location updates via device ping and GET /api/devices/:id/location/"""
        dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='352099003333001',
            brand='XIAOMI',
            model='Redmi 12',
            status=DeviceLockStatus.ACTIVE,
            is_activated=True
        )
        ping_resp = self.client.post(f'/api/devices/{dev.id}/ping/', {
            'latitude': 19.0760,
            'longitude': 72.8777,
            'address': 'Mumbai, Maharashtra, India'
        }, format='json')
        self.assertEqual(ping_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(ping_resp.data['current_location']['latitude'], 19.0760)

        loc_resp = self.client.get(f'/api/devices/{dev.id}/location/')
        self.assertEqual(loc_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(loc_resp.data['latitude'], 19.0760)
        self.assertEqual(loc_resp.data['longitude'], 72.8777)
        self.assertEqual(loc_resp.data['address'], 'Mumbai, Maharashtra, India')
        self.assertTrue(loc_resp.data['is_available'])

    def test_sim_details_endpoint_and_ping(self):
        """Tests SIM details updates via device ping and GET /api/devices/:id/sim-details/"""
        dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='352099004444001',
            brand='REALME',
            model='C55',
            status=DeviceLockStatus.ACTIVE,
            is_activated=True
        )
        ping_resp = self.client.post(f'/api/devices/{dev.id}/ping/', {
            'sim_operator': 'Jio 4G',
            'sim_number': '+919876543210',
            'sim_serial': '89918723918239123'
        }, format='json')
        self.assertEqual(ping_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(ping_resp.data['sim_details']['has_sim'])

        sim_resp = self.client.get(f'/api/devices/{dev.id}/sim-details/')
        self.assertEqual(sim_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(sim_resp.data['has_sim'])
        self.assertEqual(sim_resp.data['slot_1']['carrier'], 'Jio 4G')
        self.assertEqual(sim_resp.data['slot_1']['phone_number'], '+919876543210')
        self.assertEqual(sim_resp.data['slot_1']['iccid'], '89918723918239123')
        self.assertEqual(sim_resp.data['slot_1']['status'], 'INSERTED')

        alias_resp = self.client.get(f'/api/devices/{dev.id}/sim/')
        self.assertEqual(alias_resp.status_code, status.HTTP_200_OK)

    def test_offline_unlock_code_get_endpoints(self):
        """Tests GET endpoints for offline unlock code"""
        dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='352099005555001',
            brand='ONEPLUS',
            model='Nord CE 3',
            status=DeviceLockStatus.ACTIVE,
            is_activated=True,
            offline_unlock_code='789012'
        )
        resp1 = self.client.get(f'/api/devices/{dev.id}/offline-unlock-code/')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertEqual(resp1.data['offline_unlock_code'], '789012')

        resp2 = self.client.get(f'/api/devices/{dev.id}/offline-code/')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data['offline_unlock_code'], '789012')

        resp3 = self.client.get(f'/api/devices/{dev.id}/generate-offline-code/')
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        self.assertEqual(resp3.data['offline_unlock_code'], '789012')

    def test_offline_unlock_code_enhanced_flow(self):
        """Tests offline unlock code fields, verification, and master code"""
        dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='352099005555099',
            brand='SAMSUNG',
            model='Galaxy A14',
            status=DeviceLockStatus.LOCKED,
            is_activated=True,
            offline_unlock_code='123456'
        )

        # 1. GET returns all code aliases
        res = self.client.get(f'/api/devices/{dev.id}/offline-unlock-code/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['offline_unlock_code'], '123456')
        self.assertEqual(res.data['code'], '123456')
        self.assertEqual(res.data['unlock_code'], '123456')
        self.assertTrue(res.data.get('master_recovery_code'))
        master_code = res.data['master_recovery_code']

        # 2. Verify invalid code fails
        verify_fail = self.client.post(f'/api/devices/{dev.id}/verify-offline-code/', {'code': '000000'})
        self.assertEqual(verify_fail.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(verify_fail.data['valid'])

        # 3. Verify valid code succeeds without unlocking
        verify_ok = self.client.post(f'/api/devices/{dev.id}/verify-offline-code/', {'code': '123456'})
        self.assertEqual(verify_ok.status_code, status.HTTP_200_OK)
        self.assertTrue(verify_ok.data['valid'])
        dev.refresh_from_db()
        self.assertEqual(dev.status, DeviceLockStatus.LOCKED)

        # 4. Verify valid code with unlock_device=True unlocks the device
        verify_unlock = self.client.post(f'/api/devices/{dev.id}/verify-offline-code/', {'code': '123456', 'unlock_device': True})
        self.assertEqual(verify_unlock.status_code, status.HTTP_200_OK)
        self.assertTrue(verify_unlock.data['valid'])
        dev.refresh_from_db()
        self.assertEqual(dev.status, DeviceLockStatus.ACTIVE)

        # 5. Lock device again and verify master recovery code unlocks
        dev.status = DeviceLockStatus.LOCKED
        dev.save()
        direct_verify = self.client.post('/api/devices/verify-offline-code/', {
            'imei': dev.imei,
            'code': master_code,
            'unlock_device': True
        })
        self.assertEqual(direct_verify.status_code, status.HTTP_200_OK)
        self.assertTrue(direct_verify.data['valid'])
        self.assertTrue(direct_verify.data['is_master_code'])
        dev.refresh_from_db()
        self.assertEqual(dev.status, DeviceLockStatus.ACTIVE)

    @override_settings(PARTNER_MDM_SIMULATION=True)
    def test_partner_mdm_offline_code_dynamic_fetch(self):
        """Tests Partner MDM device dynamically retrieves 10-minute offline code"""
        pm_dev = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='356938035643809',
            brand='XIAOMI',
            model='Redmi 12',
            status=DeviceLockStatus.LOCKED,
            is_activated=True,
            lock_provider=LockProvider.PARTNER_MDM,
            provider_device_id='3501',
            offline_unlock_code='999999'
        )
        res = self.client.get(f'/api/devices/{pm_dev.id}/offline-unlock-code/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['code'], '235165')
        self.assertEqual(res.data['expiry_option'], '10_MINUTES')
        self.assertEqual(res.data['valid_until'], '10 minutes')


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

    def test_offline_code_is_six_digits(self):
        """Verify offline unlock code generated is strictly 6 digits"""
        from devices.models import generate_offline_code
        code = generate_offline_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_device_heartbeat_sim_telemetry(self):
        """Verify heartbeat ping updates SIM card information"""
        client = APIClient()
        ping_payload = {
            "imei": self.pm_device.imei,
            "battery_level": 88,
            "sim_operator": "Airtel 5G",
            "sim_number": "+919876543210",
            "sim_serial": "8991000123456789012F",
            "sim_operator_2": "Jio True 5G",
            "sim_number_2": "+919812345678"
        }
        resp = client.post('/api/devices/ping/', ping_payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.sim_operator, "Airtel 5G")
        self.assertEqual(self.pm_device.sim_number, "+919876543210")
        self.assertEqual(self.pm_device.sim_serial, "8991000123456789012F")
        self.assertEqual(self.pm_device.sim_operator_2, "Jio True 5G")
        self.assertEqual(self.pm_device.sim_number_2, "+919812345678")

    def test_sim_change_detection_and_history_logging(self):
        """Verify that SIM change is detected, logged, and surfaced in sim_details"""
        client = APIClient()
        # 1. Initial SIM (Airtel)
        initial_payload = {
            "imei": self.pm_device.imei,
            "sim_operator": "Airtel 5G",
            "sim_number": "+919876543210",
            "sim_serial": "8991000123456789012F"
        }
        resp1 = client.post('/api/devices/ping/', initial_payload, format='json')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertFalse(self.pm_device.sim_changed)

        # 2. Swap SIM to Vi with new serial
        swap_payload = {
            "imei": self.pm_device.imei,
            "sim_operator": "Vodafone Idea 4G",
            "sim_number": "+919800000000",
            "sim_serial": "8991999999999999999F"
        }
        resp2 = client.post('/api/devices/ping/', swap_payload, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()

        # Check SIM change flags and history
        self.assertTrue(self.pm_device.sim_changed)
        self.assertIsNotNone(self.pm_device.sim_change_detected_at)
        self.assertEqual(len(self.pm_device.sim_history), 1)
        self.assertEqual(self.pm_device.sim_history[0]['previous_sim']['operator'], "Airtel 5G")
        self.assertEqual(self.pm_device.sim_history[0]['new_sim']['operator'], "Vodafone Idea 4G")

        # 3. Check Device detail API includes structured sim_details
        auth_client = APIClient()
        auth_client.force_authenticate(user=self.retailer)
        dev_resp = auth_client.get(f'/api/devices/{self.pm_device.id}/')
        self.assertEqual(dev_resp.status_code, status.HTTP_200_OK)
        self.assertIn('sim_details', dev_resp.data)
        sim_data = dev_resp.data['sim_details']
        self.assertTrue(sim_data['has_sim'])
        self.assertTrue(sim_data['sim_changed'])
        self.assertEqual(sim_data['slot_1']['carrier'], "Vodafone Idea 4G")
        self.assertEqual(sim_data['slot_1']['iccid'], "8991999999999999999F")
        self.assertEqual(len(sim_data['sim_history']), 1)

        # 4. Acknowledge and clear the SIM change alert
        ack_resp = auth_client.post(f'/api/devices/{self.pm_device.id}/acknowledge-sim-change/')
        self.assertEqual(ack_resp.status_code, status.HTTP_200_OK)
        self.assertFalse(ack_resp.data['sim_changed'])
        self.pm_device.refresh_from_db()
        self.assertFalse(self.pm_device.sim_changed)

    def test_partner_mdm_qr_extraction_nesting(self):
        """Verify _extract_qr_url finds and prioritizes QR codes across provider response formats"""
        # 1. Live provider response format with 3 QRs (qr_service_app must be prioritized)
        live_payload = {
            "enrollment": {
                "qr_test_dpc": "https://apirenewjss.com/api2/QR/new/QR-1.png",
                "qr_service_app": "https://apirenewjss.com/api2/QR/new/QR-2.png",
                "qr_running_device": "https://apirenewjss.com/api2/QR/new/QR-3.png"
            }
        }
        self.assertEqual(
            PartnerMDMService._extract_qr_url(live_payload),
            "https://apirenewjss.com/api2/QR/new/QR-2.png"
        )

        # 2. Priority fallback to qr_test_dpc when qr_service_app is absent
        payload_test_dpc = {
            "enrollment": {
                "qr_test_dpc": "https://apirenewjss.com/api2/QR/new/QR-1.png",
                "qr_running_device": "https://apirenewjss.com/api2/QR/new/QR-3.png"
            }
        }
        self.assertEqual(
            PartnerMDMService._extract_qr_url(payload_test_dpc),
            "https://apirenewjss.com/api2/QR/new/QR-1.png"
        )

        # 3. Priority fallback to qr_running_device when both above are absent
        payload_running = {
            "enrollment": {
                "qr_running_device": "https://apirenewjss.com/api2/QR/new/QR-3.png"
            }
        }
        self.assertEqual(
            PartnerMDMService._extract_qr_url(payload_running),
            "https://apirenewjss.com/api2/QR/new/QR-3.png"
        )

        # 4. Standard fallback keys (e.g. qr_url, qr_code, provisioning_qr)
        data_generic = {"enrollment": {"qr_url": "https://example.com/qr1"}}
        self.assertEqual(PartnerMDMService._extract_qr_url(data_generic), "https://example.com/qr1")

        data_nested = {"device": {"id": 5354, "qr_code": "https://example.com/qr2"}}
        self.assertEqual(PartnerMDMService._extract_qr_url(data_nested), "https://example.com/qr2")

        # 5. Empty / not present
        data_empty = {"device": {"id": 5354}, "status": "OK"}
        self.assertEqual(PartnerMDMService._extract_qr_url(data_empty), "")

    def test_extract_qr_codes_all_three(self):
        """Verify _extract_qr_codes extracts all 3 QRs into a dict"""
        live_payload = {
            "enrollment": {
                "qr_test_dpc": "https://apirenewjss.com/api2/QR/new/QR-1.png",
                "qr_service_app": "https://apirenewjss.com/api2/QR/new/QR-2.png",
                "qr_running_device": "https://apirenewjss.com/api2/QR/new/QR-3.png"
            }
        }
        codes = PartnerMDMService._extract_qr_codes(live_payload)
        self.assertEqual(codes['qr_service_app'], "https://apirenewjss.com/api2/QR/new/QR-2.png")
        self.assertEqual(codes['qr_running_device'], "https://apirenewjss.com/api2/QR/new/QR-3.png")
        self.assertEqual(codes['qr_test_dpc'], "https://apirenewjss.com/api2/QR/new/QR-1.png")

    def test_device_serializer_multi_qr(self):
        """Verify DeviceSerializer exposes qr_codes and individual helper fields"""
        from devices.serializers import DeviceSerializer
        self.pm_device.qr_codes = {
            "qr_service_app": "https://example.com/qr-app",
            "qr_running_device": "https://example.com/qr-running",
            "qr_test_dpc": "https://example.com/qr-dpc"
        }
        self.pm_device.save(update_fields=['qr_codes'])
        ser = DeviceSerializer(self.pm_device)
        self.assertEqual(ser.data['qr_service_app'], "https://example.com/qr-app")
        self.assertEqual(ser.data['qr_running_device'], "https://example.com/qr-running")
        self.assertEqual(ser.data['qr_test_dpc'], "https://example.com/qr-dpc")
        self.assertIn('qr_codes', ser.data)

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

    def test_device_uninstall_blocked_when_emi_pending(self):
        from devices.models import DeviceLockStatus
        from emi.models import CustomerEMIContract, EMIContractStatus
        from decimal import Decimal
        self.pm_device.status = DeviceLockStatus.ACTIVE
        self.pm_device.save()

        # Create active contract on this device
        contract = CustomerEMIContract.objects.create(
            customer=self.pm_device.customer,
            retailer=self.pm_device.retailer,
            lock_device=self.pm_device,
            device_imei=self.pm_device.imei,
            total_amount=Decimal('10000.00'),
            down_payment=Decimal('1000.00'),
            financed_amount=Decimal('9000.00'),
            tenure_months=6,
            monthly_emi_amount=Decimal('1500.00'),
            first_emi_date=timezone.localdate(),
            status=EMIContractStatus.ACTIVE
        )

        # Retailer attempts to uninstall -> must be rejected with 400
        resp = self.client.post(f'/api/devices/{self.pm_device.id}/uninstall/', {'reason': 'Attempt before completion'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must be completed first", resp.data['detail'])

        # Now mark contract COMPLETED -> uninstall succeeds
        contract.status = EMIContractStatus.COMPLETED
        contract.save()

        resp2 = self.client.post(f'/api/devices/{self.pm_device.id}/uninstall/', {'reason': 'EMI Completed'}, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.status, DeviceLockStatus.UNENROLLED)

    def test_reset_enrollment_for_same_customer(self):
        """Verify POST /api/devices/{id}/reset-enrollment/ re-enrolls device while keeping same customer"""
        from accounts.models import AuditLog
        # 1. Setup active device with customer and old telemetry
        self.pm_device.status = DeviceLockStatus.ACTIVE
        self.pm_device.is_activated = True
        self.pm_device.fcm_token = 'old_fcm_token_xyz'
        self.pm_device.last_online_at = timezone.now()
        self.pm_device.save()

        orig_customer = self.pm_device.customer
        self.assertIsNotNone(orig_customer)

        # 2. Call reset-enrollment
        resp = self.client.post(f'/api/devices/{self.pm_device.id}/reset-enrollment/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'success')
        self.assertIn('qr_service_app', resp.data['device'])

        # 3. Verify in DB
        self.pm_device.refresh_from_db()
        self.assertEqual(self.pm_device.customer, orig_customer)
        self.assertEqual(self.pm_device.status, DeviceLockStatus.INACTIVE)
        self.assertFalse(self.pm_device.is_activated)
        self.assertIsNone(self.pm_device.last_online_at)
        self.assertEqual(self.pm_device.fcm_token, '')
        self.assertEqual(self.pm_device.provider_status, 'PENDING')
        self.assertTrue(self.pm_device.qr_code_url)

        # 4. Verify Audit Log
        log = AuditLog.objects.filter(action="DEVICE_RESET_FOR_REENROLLMENT", user=self.retailer).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.details['imei'], self.pm_device.imei)

    def test_device_offline_initially_and_online_on_ping(self):
        """Verify newly created device is offline (Never) and becomes online immediately upon ping"""
        from devices.models import Device, DeviceCategory, OSType, DeviceLockStatus, LockProvider
        # 1. New device without any ping
        new_dev = Device.objects.create(
            retailer=self.retailer,
            customer=self.pm_device.customer,
            imei="352099009876543",
            brand="Samsung",
            model="Galaxy M34",
            device_category=DeviceCategory.MOBILE,
            os_type=OSType.ANDROID,
            lock_provider=LockProvider.PARTNER_MDM,
            status=DeviceLockStatus.INACTIVE,
            is_activated=False
        )
        self.assertIsNone(new_dev.last_online_at)
        self.assertFalse(new_dev.is_online)

        # 2. Ping device via POST /api/devices/{id}/ping/
        ping_resp = self.client.post(f'/api/devices/{new_dev.id}/ping/', {
            'battery_level': 85,
            'latitude': 19.0760,
            'longitude': 72.8777
        })
        self.assertEqual(ping_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(ping_resp.data['is_online'])
        self.assertIsNotNone(ping_resp.data['last_online_at'])
        self.assertEqual(ping_resp.data['status'], DeviceLockStatus.ACTIVE)

        new_dev.refresh_from_db()
        self.assertTrue(new_dev.is_online)
        self.assertTrue(new_dev.is_activated)
        self.assertEqual(new_dev.status, DeviceLockStatus.ACTIVE)

    def test_store_real_model_not_tac_guess(self):
        """Verify explicit client model (Galaxy A14 5G) is preserved and not replaced by TAC guess"""
        from keys.models import DeviceKey, KeyStatus
        from devices.models import ApprovedDeviceTAC, TACEnvironment
        ApprovedDeviceTAC.objects.create(
            tac='35999900',
            brand='Samsung',
            model_number='Galaxy A14 5G',
            marketing_name='Galaxy A14 5G',
            environment=TACEnvironment.ALL,
            active=True
        )
        key = DeviceKey.objects.create(
            key_string='AND-MODEL-TEST-001',
            device_type='ANDROID',
            status=KeyStatus.AVAILABLE,
            current_owner=self.retailer
        )
        payload = {
            'full_name': 'Ankit Verma',
            'mobile_number': '+919866655555',
            'imei': '359999001234567',  # Unknown TAC prefix 35
            'brand': 'Samsung',
            'model': 'Galaxy A14 5G',
            'device_name': 'Samsung Galaxy A14 5G',
            'activation_key_string': key.key_string,
            'lock_provider': 'PARTNER_MDM',
            'monthly_emi': 1500,
            'number_of_emi': 6
        }
        resp = self.client.post('/api/customers/enroll/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        dev = Device.objects.get(imei='359999001234567')
        self.assertEqual(dev.model, 'Galaxy A14 5G')
        self.assertEqual(dev.brand, 'Samsung')
        self.assertEqual(dev.device_name, 'Samsung Galaxy A14 5G')


from devices.models import AppVersion, AppType

class PublicAppDownloadTests(TestCase):
    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from devices.models import AppVersion, AppType
        self.apk_content = b"PK\x03\x04MockAPKBinaryContentForTest123"
        self.apk_file = SimpleUploadedFile("app-release.apk", self.apk_content, content_type="application/vnd.android.package-archive")
        self.customer_app = AppVersion.objects.create(
            app_type=AppType.CUSTOMER_APP,
            version_name="1.0.0",
            version_code=100,
            apk_file=self.apk_file,
            is_active=True
        )
        self.client = APIClient()  # Unauthenticated client!

    def test_public_customer_apk_download_no_auth(self):
        resp = self.client.get('/api/app-versions/download/customer/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp['Content-Type'], 'application/vnd.android.package-archive')
        self.assertEqual(b"".join(resp.streaming_content), self.apk_content)

    def test_public_apk_download_unknown_slug_returns_404(self):
        resp = self.client.get('/api/app-versions/download/non-existent-app/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_app_serializer_public_link(self):
        from devices.serializers import AppVersionSerializer
        serializer = AppVersionSerializer(self.customer_app)
        self.assertIn('/api/app-versions/download/customer/', serializer.data['public_link'])
        self.assertEqual(serializer.data['download_url'], serializer.data['public_link'])

    def test_public_apk_external_redirect(self):
        AppVersion.objects.create(
            app_type=AppType.RETAILER_APP,
            version_name="2.0.0",
            version_code=200,
            external_download_url="https://cdn.primelocker.pro/retailer.apk",
            is_active=True
        )
        resp = self.client.get('/api/app-versions/download/retailer/')
        self.assertEqual(resp.status_code, status.HTTP_302_FOUND)
        self.assertEqual(resp['Location'], "https://cdn.primelocker.pro/retailer.apk")


class InstallationVideoEnrollmentOptionsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='vid_admin',
            email='vidadmin@test.com',
            password='Password@123',
            role=Role.SUPER_ADMIN
        )
        self.retailer = User.objects.create_user(
            username='vid_retailer',
            email='vidret@test.com',
            password='Password@123',
            role=Role.RETAILER,
            parent=self.admin
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(user=self.admin)
        self.ret_client = APIClient()
        self.ret_client.force_authenticate(user=self.retailer)

    def test_installation_video_enrollment_options_and_category_filter(self):
        payloads = [
            {
                'title': 'New Device Enrollment Guide',
                'category': VideoCategory.NEW_DEVICE,
                'source_type': VideoSourceType.URL,
                'video_url': 'https://www.youtube.com/watch?v=newdev123',
                'target_role': 'RETAILER',
                'display_order': 1,
                'is_active': True,
            },
            {
                'title': 'Running Device Enrollment Guide',
                'category': VideoCategory.RUNNING_DEVICE,
                'source_type': VideoSourceType.URL,
                'video_url': 'https://www.youtube.com/watch?v=rundev456',
                'target_role': 'ALL',
                'display_order': 2,
                'is_active': True,
            },
            {
                'title': 'WiFi Activation Enrollment Guide',
                'category': VideoCategory.WIFI_ACTIVATION,
                'source_type': VideoSourceType.URL,
                'video_url': 'https://www.youtube.com/watch?v=wifi789',
                'target_role': 'RETAILER',
                'display_order': 3,
                'is_active': True,
            },
        ]
        for payload in payloads:
            create_resp = self.admin_client.post('/api/installation-videos/', payload, format='json')
            self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)

        resp = self.ret_client.get('/api/installation-videos/enrollment-options/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        options = resp.data['options']
        self.assertEqual(len(options), 3)
        self.assertEqual(
            [o['category'] for o in options],
            ['NEW_DEVICE', 'RUNNING_DEVICE', 'WIFI_ACTIVATION']
        )
        self.assertEqual(options[0]['youtube_url'], 'https://www.youtube.com/watch?v=newdev123')
        self.assertEqual(options[1]['youtube_url'], 'https://www.youtube.com/watch?v=rundev456')
        self.assertEqual(options[2]['youtube_url'], 'https://www.youtube.com/watch?v=wifi789')

        filter_resp = self.ret_client.get('/api/installation-videos/?category=WIFI_ACTIVATION')
        self.assertEqual(filter_resp.status_code, status.HTTP_200_OK)
        results = filter_resp.data if isinstance(filter_resp.data, list) else filter_resp.data.get('results', [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['category'], 'WIFI_ACTIVATION')


class AppleMDMBackendTests(TestCase):
    def setUp(self):
        self.retailer = User.objects.create_user(
            username='apple_retailer',
            email='apple_retailer@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919876543210',
            business_name='Apple Store Hub'
        )
        self.customer = User.objects.create_user(
            username='apple_customer',
            email='apple_customer@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919876543211',
            parent=self.retailer
        )
        self.apple_device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='358247910271332',
            serial_number='C02XL1234567',
            brand='Apple',
            model='iPhone 15 Pro',
            os_type=OSType.IOS,
            status=DeviceLockStatus.ACTIVE,
            lock_provider=LockProvider.APPLE_MDM,
            provider_device_id='1726567890123'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.retailer)

    @patch('devices.services.apple_mdm.AppleMDMService.health_check')
    def test_apple_mdm_health_check(self, mock_health):
        mock_health.return_value = {"error": 0, "msg": "Apple MDM route is healthy 🚀 TEST"}
        resp = self.client.get('/api/devices/apple-mdm/health/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['error'], 0)

    @patch('devices.services.apple_mdm.AppleMDMService.validate_device')
    def test_apple_mdm_validate_device(self, mock_val):
        mock_val.return_value = {"error": 0, "msg": "iPhone validated successfully", "data": {"status": True}}
        resp = self.client.post('/api/devices/apple-mdm/validate/', {
            "imei": "358247910271332",
            "serial_number": "C02XL1234567"
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['data']['status'], True)

    @patch('devices.services.apple_mdm.AppleMDMService.add_device')
    def test_apple_mdm_enroll_device(self, mock_add):
        mock_add.return_value = {
            "error": 0,
            "msg": "Apple MDM device added successfully",
            "data": {"deviceRegNum": "1726567890999"}
        }
        # Pre-create un-enrolled device
        un_device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='359999999999999',
            brand='Apple',
            model='iPhone 14'
        )
        resp = self.client.post('/api/devices/apple-mdm/enroll/', {
            "imei": "359999999999999",
            "serial_number": "F17NEWDEVICE1",
            "customer_name": "Test Customer",
            "customer_mobile": "+919876543211",
            "customer_email": "customer@apple.com"
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        un_device.refresh_from_db()
        self.assertEqual(un_device.provider_device_id, '1726567890999')
        self.assertEqual(un_device.lock_provider, LockProvider.APPLE_MDM)
        self.assertEqual(un_device.os_type, OSType.IOS)

    @patch('devices.services.apple_mdm.AppleMDMService.get_devices')
    def test_apple_mdm_devices_list(self, mock_get):
        mock_get.return_value = {
            "error": 0,
            "msg": "Fetched successfully",
            "data": [{"deviceRegNum": "1726567890123"}],
            "dashboard": {"total": 1, "active": 1, "pending": 0, "inactive": 0}
        }
        resp = self.client.get('/api/devices/apple-mdm/devices/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['dashboard']['total'], 1)

    @patch('devices.services.apple_mdm.AppleMDMService.take_action')
    def test_apple_action_lock_and_unlock(self, mock_act):
        mock_act.return_value = {"error": 0, "msg": "Action LOCK performed successfully"}
        resp = self.client.post(f'/api/devices/{self.apple_device.id}/apple-action/', {
            "action": "LOCK",
            "message": "Payment overdue",
            "phoneNumber": "+919876543210"
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.LOCKED)

        mock_act.return_value = {"error": 0, "msg": "Action UNLOCK performed successfully"}
        resp2 = self.client.post(f'/api/devices/{self.apple_device.id}/apple-action/', {
            "action": "UNLOCK"
        }, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.ACTIVE)

    @patch('devices.services.apple_mdm.AppleMDMService.application_lock')
    @patch('devices.services.apple_mdm.AppleMDMService.application_unlock')
    def test_apple_app_lock_and_unlock(self, mock_unlock, mock_lock):
        mock_lock.return_value = {"error": 0, "msg": "App locked successfully"}
        mock_unlock.return_value = {"error": 0, "msg": "App unlocked successfully"}

        resp1 = self.client.post(f'/api/devices/{self.apple_device.id}/apple-app-lock/')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        mock_lock.assert_called_once_with(device_reg_num='1726567890123')

        resp2 = self.client.post(f'/api/devices/{self.apple_device.id}/apple-app-unlock/')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        mock_unlock.assert_called_once_with(device_reg_num='1726567890123')

    @patch('devices.services.apple_mdm.AppleMDMService.activation_lock')
    @patch('devices.services.apple_mdm.AppleMDMService.activation_lock_status')
    def test_apple_activation_lock_and_status(self, mock_status, mock_act):
        mock_act.return_value = {"error": 0, "msg": "Activation Lock enabled"}
        mock_status.return_value = {"error": 0, "data": {"status": True}}

        resp1 = self.client.post(f'/api/devices/{self.apple_device.id}/apple-activation-lock/', {"action": 1})
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)

        resp2 = self.client.get(f'/api/devices/{self.apple_device.id}/apple-activation-lock-status/')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)

    @patch('devices.services.apple_mdm.AppleMDMService.set_wallpaper')
    @patch('devices.services.apple_mdm.AppleMDMService.remove_wallpaper')
    def test_apple_wallpaper_set_and_remove(self, mock_remove, mock_set):
        mock_set.return_value = {"error": 0, "msg": "Wallpaper set successfully"}
        mock_remove.return_value = {"error": 0, "msg": "Wallpaper removed successfully"}

        resp1 = self.client.post(f'/api/devices/{self.apple_device.id}/apple-wallpaper/', {
            "wallpaper_image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        }, format='json')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertTrue(self.apple_device.wallpaper_locked)

        resp2 = self.client.post(f'/api/devices/{self.apple_device.id}/apple-wallpaper/', {
            "action": "REMOVE"
        }, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertFalse(self.apple_device.wallpaper_locked)

    @patch('devices.services.apple_mdm.AppleMDMService.take_action')
    def test_standard_lock_unlock_delegates_to_apple_mdm(self, mock_action):
        mock_action.return_value = {"error": 0, "msg": "Success"}
        lock_resp = self.client.post(f'/api/devices/{self.apple_device.id}/lock/', {"reason": "Overdue"})
        self.assertEqual(lock_resp.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.LOCKED)
        mock_action.assert_called_with(
            device_reg_num='1726567890123',
            action='LOCK',
            message='Overdue',
            phone_number='+919876543210',
            foot_note='Prime Locker Protection'
        )

        unlock_resp = self.client.post(f'/api/devices/{self.apple_device.id}/unlock/')
        self.assertEqual(unlock_resp.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.ACTIVE)
        mock_action.assert_called_with(
            device_reg_num='1726567890123',
            action='UNLOCK'
        )

    def test_apple_mdm_webhook_processing(self):
        # 1. Test lock status callback
        payload_lock = {
            "event": "apple_device_status_update",
            "callback_id": "test-uuid-1",
            "deviceRegNum": "1726567890123",
            "slug": "lock_status",
            "status": 1,
            "occurred_at": "2026-11-20T10:00:00Z"
        }
        anon_client = APIClient()
        resp1 = anon_client.post('/api/integrations/apple-mdm/webhook/', payload_lock, format='json')
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.LOCKED)

        # 2. Test unlock status callback
        payload_unlock = {
            "event": "apple_device_status_update",
            "callback_id": "test-uuid-2",
            "deviceRegNum": "1726567890123",
            "slug": "lock_status",
            "status": 0,
            "occurred_at": "2026-11-20T10:01:00Z"
        }
        resp2 = anon_client.post('/api/integrations/apple-mdm/webhook/', payload_unlock, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.ACTIVE)

        # 3. Test location callback
        payload_loc = {
            "event": "apple_device_status_update",
            "callback_id": "test-uuid-3",
            "deviceRegNum": "1726567890123",
            "slug": "device_location",
            "status": None,
            "location": {
                "lat": "28.6139000",
                "long": "77.2090000",
                "date": "20-11-2026 15:30:00"
            },
            "occurred_at": "2026-11-20T10:02:00Z"
        }
        resp3 = anon_client.post('/api/integrations/apple-mdm/webhook/', payload_loc, format='json')
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertAlmostEqual(float(self.apple_device.current_latitude), 28.6139, places=4)
        self.assertAlmostEqual(float(self.apple_device.current_longitude), 77.2090, places=4)

    @patch('devices.services.apple_mdm.AppleMDMService.update_device')
    @patch('devices.services.apple_mdm.AppleMDMService.add_emi_details')
    def test_apple_sync_customer_and_emi(self, mock_add_emi, mock_update):
        from emi.models import CustomerEMIContract, EMIContractStatus
        mock_update.return_value = {"error": 0, "msg": "Device updated"}
        mock_add_emi.return_value = {"error": 0, "msg": "EMI details added"}

        contract = CustomerEMIContract.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            device_imei=self.apple_device.imei,
            lock_device=self.apple_device,
            total_amount=Decimal('15000.00'),
            tenure_months=6,
            monthly_emi_amount=Decimal('2500.00'),
            financed_amount=Decimal('15000.00'),
            first_emi_date=timezone.localdate(),
            status=EMIContractStatus.ACTIVE
        )
        self.apple_device.emi_contract = contract
        self.apple_device.save()

        resp = self.client.post(f'/api/devices/{self.apple_device.id}/apple-sync-customer/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('device_sync', resp.data['results'])
        self.assertIn('emi_sync', resp.data['results'])
        mock_update.assert_called_once()
        mock_add_emi.assert_called_once()

    @patch('devices.services.apple_mdm.AppleMDMService.take_action')
    def test_emi_contract_lock_unlock_delegates_to_apple_mdm(self, mock_act):
        from emi.models import CustomerEMIContract, EMIContractStatus
        mock_act.return_value = {"error": 0, "msg": "Action LOCK performed"}

        contract = CustomerEMIContract.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            device_imei=self.apple_device.imei,
            lock_device=self.apple_device,
            total_amount=Decimal('15000.00'),
            tenure_months=6,
            monthly_emi_amount=Decimal('2500.00'),
            financed_amount=Decimal('15000.00'),
            first_emi_date=timezone.localdate(),
            status=EMIContractStatus.ACTIVE
        )
        self.apple_device.emi_contract = contract
        self.apple_device.save()

        # Lock from EMI contract
        resp_lock = self.client.post(f'/api/emi-contracts/{contract.id}/lock/', {"reason": "Overdue payment"})
        self.assertEqual(resp_lock.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.LOCKED)
        mock_act.assert_called_with(
            device_reg_num='1726567890123',
            action='LOCK',
            message='Overdue payment',
            phone_number='+919876543210',
            foot_note='Prime Locker Protection'
        )

        # Unlock from EMI contract
        mock_act.return_value = {"error": 0, "msg": "Action UNLOCK performed"}
        resp_unlock = self.client.post(f'/api/emi-contracts/{contract.id}/unlock/', {"reason": "Payment received"})
        self.assertEqual(resp_unlock.status_code, status.HTTP_200_OK)
        self.apple_device.refresh_from_db()
        self.assertEqual(self.apple_device.status, DeviceLockStatus.ACTIVE)
        mock_act.assert_called_with(
            device_reg_num='1726567890123',
            action='UNLOCK'
        )


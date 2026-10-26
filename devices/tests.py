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
    TACEnvironment
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

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import Role, PlatformSettings
from devices.models import ApprovedDeviceTAC, TACEnvironment
from keys.models import DeviceKey, KeyStatus

User = get_user_model()


@override_settings(ROCKETPAY_SIMULATION=True)
class AccountsBackendTests(TestCase):
    def setUp(self):
        self.retailer = User.objects.create_user(
            username='test_retailer_acc',
            email='retailer_acc@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919855555555',
            business_name='Initial Store Name',
            address='123 Main Street',
            city='Bengaluru',
            store_phone='+9180222222'
        )

        # Set up PlatformSettings in STAGING mode for testing
        ps = PlatformSettings.get_settings()
        ps.rocketpay_base_url = 'https://api-staging.rocketpay.co.in'
        ps.save()

        # Create Approved TAC
        self.approved_tac = ApprovedDeviceTAC.objects.create(
            tac='86667106',
            brand='VIVO',
            model_number='V2205',
            marketing_name='Y35',
            environment=TACEnvironment.STAGING,
            active=True
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.retailer)

    def test_partner_business_profile_patch(self):
        """Tests PATCH /api/auth/me/ updates name, business_name, address, city, store_phone"""
        url = '/api/auth/me/'
        update_data = {
            'name': 'Updated Retailer Name',
            'business_name': 'New Royal Electronics',
            'address': '456 MG Road',
            'city': 'Mysuru',
            'store_phone': '+91821444444'
        }
        resp = self.client.patch(url, update_data, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.retailer.refresh_from_db()
        self.assertEqual(self.retailer.first_name, 'Updated')
        self.assertEqual(self.retailer.last_name, 'Retailer Name')
        self.assertEqual(self.retailer.business_name, 'New Royal Electronics')
        self.assertEqual(self.retailer.address, '456 MG Road')
        self.assertEqual(self.retailer.city, 'Mysuru')
        self.assertEqual(self.retailer.store_phone, '+91821444444')

    def test_tac_list_check_endpoint(self):
        """Tests GET /api/tac-list/check/?imei=... returns approved or rejected status"""
        # Approved IMEI (TAC: 86667106)
        resp_approved = self.client.get('/api/tac-list/check/?imei=866671069999999')
        self.assertEqual(resp_approved.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_approved.data.get('eligible'))
        self.assertEqual(resp_approved.data.get('brand'), 'VIVO')

        # Unapproved IMEI (TAC: 99999999)
        resp_unapproved = self.client.get('/api/tac-list/check/?imei=999999990000000')
        self.assertEqual(resp_unapproved.status_code, status.HTTP_200_OK)
        self.assertFalse(resp_unapproved.data.get('eligible'))

    def test_enrollment_rejects_unapproved_tac(self):
        """Tests that customer enrollment rejects phones with unapproved TAC codes"""
        # Create an available key for the retailer
        key = DeviceKey.objects.create(
            key_string='AND-TEST-TAC-001',
            device_type='ANDROID',
            status=KeyStatus.AVAILABLE,
            current_owner=self.retailer
        )

        enroll_url = '/api/customers/enroll/'
        # Use an unapproved IMEI (TAC: 35000000)
        unapproved_payload = {
            'full_name': 'Test Unapproved Customer',
            'mobile_number': '+919866666666',
            'imei': '350000001234567',
            'brand': 'UnknownBrand',
            'model': 'UnknownModel',
            'activation_key_string': key.key_string,
            'lock_provider': 'ROCKETPAY'
        }

        resp = self.client.post(enroll_url, unapproved_payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not in the approved list', resp.data.get('detail', ''))

        # Verify key was not activated and was kept available
        key.refresh_from_db()
        self.assertEqual(key.status, KeyStatus.AVAILABLE)

    def test_dpc_enrollment_returns_qr_code(self):
        """Tests RP5: Enrolling device with mdm_type=DPC returns qr_code_data"""
        key = DeviceKey.objects.create(
            key_string='AND-TEST-DPC-001',
            device_type='ANDROID',
            status=KeyStatus.AVAILABLE,
            current_owner=self.retailer
        )
        payload = {
            'full_name': 'Test DPC Customer',
            'mobile_number': '+919877777777',
            'imei': '866671060005555',
            'brand': 'VIVO',
            'model': 'Y35',
            'activation_key_string': key.key_string,
            'lock_provider': 'ROCKETPAY',
            'mdm_type': 'DPC',
            'frp_email': 'customer@gmail.com'
        }
        resp = self.client.post('/api/customers/enroll/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn('device', resp.data)
        self.assertTrue(resp.data['device'].get('qr_code_data'))

    def test_superadmin_dashboard_shows_rocketpay_wallet(self):
        """Tests RP11: Super Admin dashboard returns RocketPay wallet key balance"""
        admin = User.objects.create_superuser(
            username='test_admin_wallet',
            email='admin_wallet@test.com',
            password='Password@123',
            role=Role.SUPER_ADMIN
        )
        client = APIClient()
        client.force_authenticate(user=admin)
        resp = client.get('/api/dashboard/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('rocketpay_wallet', resp.data)
        self.assertIsNotNone(resp.data['rocketpay_wallet'])

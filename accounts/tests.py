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

    def test_duplicate_mobile_numbers_login_does_not_crash(self):
        """Tests that two accounts sharing mobile number identifier authenticate correctly without 500 server crash"""
        shared_number = '9988112233'
        user_a = User.objects.create_user(
            username=shared_number,
            email='user_a@test.com',
            password='PasswordA@123',
            role=Role.CUSTOMER,
            mobile_number='+919988112233',
            parent=self.retailer
        )
        user_b = User.objects.create_user(
            username='user_shared_b',
            email='user_b@test.com',
            password='PasswordB@123',
            role=Role.CUSTOMER,
            mobile_number=shared_number,
            parent=self.retailer
        )

        login_client = APIClient()

        # Login with shared mobile and User A's password
        resp_a = login_client.post('/api/auth/login/', {'username': shared_number, 'password': 'PasswordA@123'}, format='json')
        self.assertEqual(resp_a.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_a.data['user']['username'], shared_number)

        # Login with shared mobile and User B's password
        resp_b = login_client.post('/api/auth/login/', {'username': shared_number, 'password': 'PasswordB@123'}, format='json')
        self.assertEqual(resp_b.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_b.data['user']['username'], 'user_shared_b')

        # Login with wrong password returns 401, not 500 crash
        resp_fail = login_client.post('/api/auth/login/', {'username': shared_number, 'password': 'WrongPassword@999'}, format='json')
        self.assertEqual(resp_fail.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_store_resets_customer_password(self):
        """Tests that a store retailer can reset password for their enrolled customer"""
        customer = User.objects.create_user(
            username='cust_pwd_reset',
            email='cust_pwd@test.com',
            password='OldPassword@123',
            role=Role.CUSTOMER,
            mobile_number='+919876500011',
            parent=self.retailer
        )

        reset_url = f'/api/customers/{customer.id}/reset-password/'
        resp = self.client.post(reset_url, {'new_password': 'BrandNewPassword@456'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['success'])

        # Verify customer can now log in with the new password
        login_client = APIClient()
        resp_login = login_client.post('/api/auth/login/', {'username': customer.username, 'password': 'BrandNewPassword@456'}, format='json')
        self.assertEqual(resp_login.status_code, status.HTTP_200_OK)
        self.assertIn('access', resp_login.data)

        # Another retailer cannot reset this customer's password (out of store scope -> 404 or 403)
        other_retailer = User.objects.create_user(
            username='other_retailer_pwd',
            email='other_retailer@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919876500022'
        )
        other_client = APIClient()
        other_client.force_authenticate(user=other_retailer)
        resp_other = other_client.post(reset_url, {'new_password': 'HackerPassword@123'}, format='json')
        self.assertIn(resp_other.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

        # A customer cannot reset another customer's password -> 403 Forbidden
        customer_client = APIClient()
        customer_client.force_authenticate(user=customer)
        resp_cust = customer_client.post(reset_url, {'new_password': 'HackerPassword@123'}, format='json')
        self.assertIn(resp_cust.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    def test_distributor_resets_downstream_customer_password(self):
        """Tests that a distributor can reset password for customer in their downline without 500"""
        distributor = User.objects.create_user(
            username='test_distributor_up',
            email='distributor_up@test.com',
            password='Password@123',
            role=Role.DISTRIBUTOR,
            mobile_number='+919876599001'
        )
        # Retailer is child of distributor
        self.retailer.parent = distributor
        self.retailer.save()

        customer = User.objects.create_user(
            username='cust_dist_downline',
            email='cust_downline@test.com',
            password='OldPassword@123',
            role=Role.CUSTOMER,
            mobile_number='+919876599002',
            parent=self.retailer
        )

        dist_client = APIClient()
        dist_client.force_authenticate(user=distributor)

        reset_url = f'/api/customers/{customer.id}/reset-password/'
        resp = dist_client.post(reset_url, {'new_password': 'DistributorReset@123'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['success'])

        customer.refresh_from_db()
        self.assertTrue(customer.check_password('DistributorReset@123'))

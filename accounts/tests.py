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

    def test_customer_lifecycle_inactive_editable_active_locked(self):
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_lifecycle_test',
            email='lifecycle@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919876543210',
            address='Initial Street',
            parent=self.retailer
        )
        device = Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='999988887777666',
            brand='Samsung',
            model='Galaxy A15',
            status=DeviceLockStatus.INACTIVE,
            is_activated=False
        )
        client = APIClient()
        client.force_authenticate(user=self.retailer)
        detail_resp = client.get(f'/api/customers/{customer.id}/')
        self.assertEqual(detail_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_resp.data['status'], 'INACTIVE')

        # Retailer edits while INACTIVE -> succeeds
        patch_resp = client.patch(f'/api/customers/{customer.id}/', {'address': 'Updated Street 1'}, format='json')
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        customer.refresh_from_db()
        self.assertEqual(customer.address, 'Updated Street 1')

        # Device is activated
        device.is_activated = True
        device.status = DeviceLockStatus.ACTIVE
        device.save()

        # Status is now ACTIVE
        detail_resp2 = client.get(f'/api/customers/{customer.id}/')
        self.assertEqual(detail_resp2.data['status'], 'ACTIVE')

        # Retailer tries to edit while ACTIVE -> returns 400
        patch_resp2 = client.patch(f'/api/customers/{customer.id}/', {'address': 'Blocked Street'}, format='json')
        self.assertEqual(patch_resp2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cannot be modified once the device is activated", patch_resp2.data['detail'])

    def test_uninstalled_customer_status_and_dashboard_filter(self):
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_uninstalled_test',
            email='uninstalled@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919876543211',
            parent=self.retailer
        )
        device = Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='999988887777555',
            brand='Vivo',
            model='Y20',
            status=DeviceLockStatus.UNENROLLED,
            provider_status='UNREGISTERED'
        )
        client = APIClient()
        client.force_authenticate(user=self.retailer)
        
        detail_resp = client.get(f'/api/customers/{customer.id}/')
        self.assertEqual(detail_resp.data['status'], 'UNINSTALL')

        list_resp = client.get('/api/customers/?status=uninstalled')
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        ids = [c['id'] for c in list_resp.data.get('results', list_resp.data)]
        self.assertIn(customer.id, ids)

        dash_resp = client.get('/api/dashboard/')
        self.assertEqual(dash_resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(dash_resp.data['device_and_emi_kpis']['uninstalled_customers'], 1)

    def test_retailer_terms_and_conditions_endpoint(self):
        ps = PlatformSettings.get_settings()
        ps.retailer_terms_and_conditions = "Prime Locker Retailer Agreement: All devices must be verified."
        ps.save()

        client = APIClient()
        client.force_authenticate(user=self.retailer)
        resp = client.get('/api/settings/terms-and-conditions/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("Prime Locker Retailer Agreement", resp.data['terms_and_conditions'])

    def test_unified_customer_and_device_details_consistency(self):
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_unified_test',
            email='unified@test.com',
            first_name='Rahul',
            last_name='Sharma',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919876543212',
            address='MG Road 123',
            city='Bengaluru',
            state='Karnataka',
            pincode='560001',
            id_type='AADHAAR',
            id_number='123456789012',
            parent=self.retailer
        )
        device = Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='999988887777444',
            brand='Xiaomi',
            model='Redmi Note 12',
            status=DeviceLockStatus.ACTIVE,
            is_activated=True
        )
        client = APIClient()
        client.force_authenticate(user=self.retailer)
        
        cust_resp = client.get(f'/api/customers/{customer.id}/')
        dev_resp = client.get(f'/api/devices/{device.id}/')

        cust_data = cust_resp.data
        dev_cust = dev_resp.data['customer_details']

        self.assertEqual(cust_data['name'], dev_cust['name'])
        self.assertEqual(cust_data['mobile_number'], dev_cust['mobile_number'])
        self.assertEqual(cust_data['address'], dev_cust['address'])
        self.assertEqual(cust_data['city'], dev_cust['city'])
        self.assertEqual(cust_data['state'], dev_cust['state'])
        self.assertEqual(cust_data['pincode'], dev_cust['pincode'])
        self.assertEqual(cust_data['status'], dev_cust['status'])

    def test_device_activate_auth_with_customer_code(self):
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_dev_act_1',
            email='devact1@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919111222333',
            parent=self.retailer
        )
        customer.customer_code = f"PR-{customer.id:04d}"
        customer.save()

        device = Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='864201234567891',
            brand='Samsung',
            model='Galaxy A15',
            status=DeviceLockStatus.INACTIVE,
            is_activated=False
        )

        client = APIClient()
        # Test with uppercase PR-XXXX
        resp = client.post('/auth/device-activate/', {
            'customer_id': customer.customer_code,
            'fcm_token': 'fcm_token_sample_abc123',
            'imei': '864201234567891',
            'app_version': '1.0.2'
        }, format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)
        self.assertEqual(resp.data['user']['id'], customer.id)
        self.assertEqual(resp.data['user']['customer_code'], customer.customer_code)
        self.assertEqual(resp.data['device']['status'], DeviceLockStatus.ACTIVE)
        self.assertTrue(resp.data['device']['is_activated'])

        device.refresh_from_db()
        self.assertEqual(device.fcm_token, 'fcm_token_sample_abc123')
        self.assertTrue(device.is_activated)
        self.assertEqual(device.status, DeviceLockStatus.ACTIVE)

    def test_device_activate_auth_with_lowercase_code(self):
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_dev_act_2',
            email='devact2@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919111222334',
            parent=self.retailer
        )
        customer.customer_code = f"PR-{customer.id:04d}"
        customer.save()

        Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='864201234567892',
            status=DeviceLockStatus.INACTIVE
        )

        client = APIClient()
        # Test with lowercase pr-xxxx
        resp = client.post('/auth/device-activate/', {
            'customer_id': customer.customer_code.lower()
        }, format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['user']['id'], customer.id)

    def test_device_activate_auth_with_mobile_number(self):
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_dev_act_3',
            email='devact3@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919111222335',
            parent=self.retailer
        )
        Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='864201234567893',
            status=DeviceLockStatus.INACTIVE
        )

        client = APIClient()
        resp = client.post('/auth/device-activate/', {
            'customer_id': '9111222335'
        }, format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['user']['id'], customer.id)

    def test_device_activate_invalid_id_returns_404(self):
        client = APIClient()
        resp = client.post('/auth/device-activate/', {
            'customer_id': 'PR-NONEXISTENT-9999'
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Customer not found", resp.data['detail'])

    def test_device_activate_by_imei(self):
        """Verify device activation succeeds when customer_id is the device's IMEI"""
        from devices.models import Device, DeviceLockStatus
        customer = User.objects.create_user(
            username='cust_imei_act',
            email='cust_imei_act@example.com',
            mobile_number='9888777666',
            role='CUSTOMER',
            customer_code='PR-7788'
        )
        device = Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='861803254940484',
            secondary_imei='861803254940485',
            status=DeviceLockStatus.INACTIVE
        )

        client = APIClient()
        resp = client.post('/auth/device-activate/', {
            'customer_id': '861803254940484',
            'fcm_token': 'fcm_running_token_123'
        }, format='json')

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['user']['id'], customer.id)
        self.assertIn('access', resp.data)
        device.refresh_from_db()
        self.assertTrue(device.is_activated)
        self.assertEqual(device.fcm_token, 'fcm_running_token_123')


@override_settings(ROCKETPAY_SIMULATION=True, PARTNER_MDM_SIMULATION=True)
class PrimeLockerProRequirementsTests(TestCase):
    def setUp(self):
        self.distributor = User.objects.create_user(
            username='dist_pro',
            email='dist_pro@test.com',
            password='Password@123',
            role=Role.DISTRIBUTOR,
            mobile_number='+919700000001'
        )
        self.retailer = User.objects.create_user(
            username='ret_pro',
            email='ret_pro@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919855551234',
            business_name='Prime Pro Mobiles',
            first_name='Ramesh',
            last_name='Kumar',
            parent=self.distributor
        )
        ps = PlatformSettings.get_settings()
        ps.rocketpay_base_url = 'https://api-staging.rocketpay.co.in'
        ps.save()

        ApprovedDeviceTAC.objects.create(
            tac='86667106',
            brand='VIVO',
            model_number='V2205',
            marketing_name='Y35',
            environment=TACEnvironment.STAGING,
            active=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.retailer)

    def test_enroll_blocked_on_zero_balance_and_allowed_after_key_allocation(self):
        from keys.services import ZERO_KEY_MESSAGE

        base_payload = {
            'full_name': 'Suresh Customer',
            'mobile_number': '9911223344',
            'imei': '866671061111222',
            'brand': 'VIVO',
            'model': 'Y35',
        }

        # Zero balance across all 3 services blocks enrollment
        for svc_payload, expected_service in [
            ({'mdm_type': 'PARTNER_MDM'}, 'Android MDM'),
            ({'mdm_type': 'DLC'}, 'Android DLC Locker'),
            ({'os_type': 'IOS'}, 'iOS Locker'),
        ]:
            resp = self.client.post('/api/customers/enroll/', {**base_payload, **svc_payload}, format='json')
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(resp.data['error_code'], 'ZERO_KEY_BALANCE')
            self.assertEqual(resp.data['available_balance'], 0)
            self.assertFalse(resp.data['can_enroll'])
            self.assertEqual(resp.data['service'], expected_service)
            self.assertEqual(resp.data['detail'], ZERO_KEY_MESSAGE)

        # Allocate 1 DLC key to retailer
        dlc_key = DeviceKey.objects.create(
            key_string='DLC-TEST-9001',
            device_type='DLC',
            current_owner=self.retailer,
            status=KeyStatus.AVAILABLE,
        )

        # Android MDM still has 0 balance and stays blocked
        resp_mdm = self.client.post('/api/customers/enroll/', {**base_payload, 'mdm_type': 'PARTNER_MDM'}, format='json')
        self.assertEqual(resp_mdm.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp_mdm.data['service'], 'Android MDM')

        # Android DLC Locker now has 1 key and succeeds, auto-consuming the DLC key
        resp_dlc = self.client.post('/api/customers/enroll/', {**base_payload, 'mdm_type': 'DLC'}, format='json')
        self.assertEqual(resp_dlc.status_code, status.HTTP_201_CREATED)
        dlc_key.refresh_from_db()
        self.assertEqual(dlc_key.status, KeyStatus.ACTIVATED)

        bal_after = self.client.get('/api/keys/check-balance/?service=ANDROID_DLC')
        self.assertEqual(bal_after.data['available_balance'], 0)
        self.assertFalse(bal_after.data['can_enroll'])

    def test_customer_can_edit_flag_before_and_after_activation(self):
        from devices.models import Device, DeviceLockStatus

        customer = User.objects.create_user(
            username='cust_dyn_edit',
            email='custdyn@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919922334455',
            first_name='Before',
            last_name='Activation',
            parent=self.retailer
        )
        device = Device.objects.create(
            retailer=self.retailer,
            customer=customer,
            imei='866671069998887',
            status=DeviceLockStatus.INACTIVE,
            is_activated=False
        )

        # Before activation: editable
        detail_before = self.client.get(f'/api/customers/{customer.id}/')
        self.assertEqual(detail_before.status_code, status.HTTP_200_OK)
        self.assertTrue(detail_before.data['can_edit'])
        self.assertFalse(detail_before.data['is_device_activated'])

        patch_before = self.client.patch(f'/api/customers/{customer.id}/', {'city': 'Pune'}, format='json')
        self.assertEqual(patch_before.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_before.data['city'], 'Pune')

        # After activation: read-only for retailer
        device.is_activated = True
        device.status = DeviceLockStatus.ACTIVE
        device.save()

        detail_after = self.client.get(f'/api/customers/{customer.id}/')
        self.assertEqual(detail_after.status_code, status.HTTP_200_OK)
        self.assertFalse(detail_after.data['can_edit'])
        self.assertTrue(detail_after.data['is_device_activated'])

        patch_after = self.client.patch(f'/api/customers/{customer.id}/', {'city': 'Mumbai'}, format='json')
        self.assertIn(patch_after.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])
        customer.refresh_from_db()
        self.assertEqual(customer.city, 'Pune')

    def test_forgot_password_mobile_otp_verify_and_confirm(self):
        from accounts.models import PasswordResetOTP

        anon = APIClient()

        # Request OTP via normalized 10-digit mobile number (stored with +91)
        req_resp = anon.post('/api/auth/password-reset-request/', {'mobile_number': '9855551234'}, format='json')
        self.assertEqual(req_resp.status_code, status.HTTP_200_OK)

        otp_obj = PasswordResetOTP.objects.filter(user=self.retailer, purpose='PASSWORD_RESET', is_used=False).latest('created_at')

        # Wrong OTP fails verification
        bad_verify = anon.post('/api/auth/password-reset-verify-otp/', {'mobile_number': '9855551234', 'otp': '000000'}, format='json')
        self.assertEqual(bad_verify.status_code, status.HTTP_400_BAD_REQUEST)

        # Valid OTP passes standalone verification step without marking OTP used
        good_verify = anon.post('/api/auth/password-reset-verify-otp/', {'mobile_number': '9855551234', 'otp': otp_obj.otp}, format='json')
        self.assertEqual(good_verify.status_code, status.HTTP_200_OK)
        self.assertTrue(good_verify.data['valid'])

        # Mismatched confirm fails
        mismatch_resp = anon.post('/api/auth/password-reset-confirm/', {
            'mobile_number': '9855551234',
            'otp': otp_obj.otp,
            'new_password': 'NewStrong@123',
            'confirm_password': 'Different@123',
        }, format='json')
        self.assertEqual(mismatch_resp.status_code, status.HTTP_400_BAD_REQUEST)

        # Matching confirm succeeds and marks OTP used
        confirm_resp = anon.post('/api/auth/password-reset-confirm/', {
            'mobile_number': '9855551234',
            'otp': otp_obj.otp,
            'new_password': 'NewStrong@123',
            'confirm_password': 'NewStrong@123',
        }, format='json')
        self.assertEqual(confirm_resp.status_code, status.HTTP_200_OK)

        # Cannot reuse same OTP
        reuse_resp = anon.post('/api/auth/password-reset-confirm/', {
            'mobile_number': '9855551234',
            'otp': otp_obj.otp,
            'new_password': 'AnotherStrong@123',
            'confirm_password': 'AnotherStrong@123',
        }, format='json')
        self.assertEqual(reuse_resp.status_code, status.HTTP_400_BAD_REQUEST)

        # Login with new password succeeds
        login_resp = anon.post('/api/auth/login/', {'username': '9855551234', 'password': 'NewStrong@123'}, format='json')
        self.assertEqual(login_resp.status_code, status.HTTP_200_OK)
        self.assertIn('access', login_resp.data)

    def test_mpin_full_lifecycle_and_forgot_reset(self):
        from accounts.models import PasswordResetOTP

        anon = APIClient()

        # 1. First-time login indicates has_mpin=False
        first_login = anon.post('/api/auth/login/', {'username': 'ret_pro', 'password': 'Password@123'}, format='json')
        self.assertEqual(first_login.status_code, status.HTTP_200_OK)
        self.assertFalse(first_login.data['has_mpin'])
        self.assertFalse(first_login.data['user']['has_mpin'])
        self.assertEqual(first_login.data['user']['business_name'], 'Prime Pro Mobiles')

        # MPIN login rejected cleanly before setup
        pre_mpin_login = anon.post('/api/auth/mpin/login/', {'mobile_number': '9855551234', 'mpin': '1234'}, format='json')
        self.assertEqual(pre_mpin_login.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(pre_mpin_login.data['has_mpin'])

        # 2. Setup validation: reject 5-digit, non-numeric, and mismatched confirm
        token_client = APIClient()
        token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {first_login.data['access']}")

        for bad_payload in [
            {'mpin': '12345', 'confirm_mpin': '12345'},
            {'mpin': 'abcd', 'confirm_mpin': 'abcd'},
            {'mpin': '1234', 'confirm_mpin': '4321'},
        ]:
            bad_setup = token_client.post('/api/auth/mpin/setup/', bad_payload, format='json')
            self.assertEqual(bad_setup.status_code, status.HTTP_400_BAD_REQUEST)

        # Setup 4-digit MPIN then update to 6-digit MPIN
        setup_4 = token_client.post('/api/auth/mpin/setup/', {'mpin': '1234', 'confirm_mpin': '1234'}, format='json')
        self.assertEqual(setup_4.status_code, status.HTTP_200_OK)
        self.assertTrue(setup_4.data['has_mpin'])

        setup_6 = token_client.post('/api/auth/mpin/setup/', {'mpin': '654321', 'confirm_mpin': '654321'}, format='json')
        self.assertEqual(setup_6.status_code, status.HTTP_200_OK)
        self.assertTrue(setup_6.data['has_mpin'])

        # Subsequent password login also sees has_mpin=True
        second_login = anon.post('/api/auth/login/', {'username': 'ret_pro', 'password': 'Password@123'}, format='json')
        self.assertTrue(second_login.data['has_mpin'])

        # 3. Direct MPIN Login
        wrong_mpin = anon.post('/api/auth/mpin/login/', {'mobile_number': '9855551234', 'mpin': '000000'}, format='json')
        self.assertEqual(wrong_mpin.status_code, status.HTTP_401_UNAUTHORIZED)

        good_mpin = anon.post('/api/auth/mpin/login/', {'mobile_number': '9855551234', 'mpin': '654321'}, format='json')
        self.assertEqual(good_mpin.status_code, status.HTTP_200_OK)
        self.assertIn('access', good_mpin.data)
        self.assertIn('refresh', good_mpin.data)
        self.assertTrue(good_mpin.data['has_mpin'])
        self.assertEqual(good_mpin.data['user']['username'], 'ret_pro')

        # Verify issued JWT works against /api/auth/me/
        mpin_authed = APIClient()
        mpin_authed.credentials(HTTP_AUTHORIZATION=f"Bearer {good_mpin.data['access']}")
        me_resp = mpin_authed.get('/api/auth/me/')
        self.assertEqual(me_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(me_resp.data['has_mpin'])

        # 4. Forgot MPIN + Reset invalidates old MPIN
        forgot_resp = anon.post('/api/auth/mpin/forgot/', {'mobile_number': '+91 98555 51234'}, format='json')
        self.assertEqual(forgot_resp.status_code, status.HTTP_200_OK)
        mpin_otp = PasswordResetOTP.objects.filter(user=self.retailer, purpose='MPIN_RESET', is_used=False).latest('created_at')

        # Purpose isolation: MPIN_RESET OTP cannot be used on password-reset-verify-otp
        cross_purpose = anon.post('/api/auth/password-reset-verify-otp/', {'mobile_number': '9855551234', 'otp': mpin_otp.otp}, format='json')
        self.assertEqual(cross_purpose.status_code, status.HTTP_400_BAD_REQUEST)

        # Reset MPIN
        reset_resp = anon.post('/api/auth/mpin/reset/', {
            'mobile_number': '9855551234',
            'otp': mpin_otp.otp,
            'new_mpin': '998877',
            'confirm_mpin': '998877',
        }, format='json')
        self.assertEqual(reset_resp.status_code, status.HTTP_200_OK)

        # Old MPIN invalidated
        old_mpin_login = anon.post('/api/auth/mpin/login/', {'mobile_number': '9855551234', 'mpin': '654321'}, format='json')
        self.assertEqual(old_mpin_login.status_code, status.HTTP_401_UNAUTHORIZED)

        # OTP cannot be reused
        reuse_otp = anon.post('/api/auth/mpin/reset/', {
            'mobile_number': '9855551234',
            'otp': mpin_otp.otp,
            'new_mpin': '112233',
            'confirm_mpin': '112233',
        }, format='json')
        self.assertEqual(reuse_otp.status_code, status.HTTP_400_BAD_REQUEST)

        # New MPIN works
        new_mpin_login = anon.post('/api/auth/mpin/login/', {'mobile_number': '9855551234', 'mpin': '998877'}, format='json')
        self.assertEqual(new_mpin_login.status_code, status.HTTP_200_OK)
        self.assertIn('access', new_mpin_login.data)

    def test_jio_dlt_otp_sms_dispatch_and_validity(self):
        """Verifies Jio DLT compliant OTP SMS format (PRLOCK / 1277179438043729654) and 5-min validity."""
        from unittest.mock import patch
        import json
        from accounts.views import _send_otp_sms
        from accounts.models import PasswordResetOTP
        from django.utils import timezone
        from datetime import timedelta

        # 1. Test DLT payload structure and exact template text
        captured_requests = []

        class DummyResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def mock_urlopen(req, timeout=None):
            captured_requests.append(req)
            return DummyResponse()

        with patch.dict('os.environ', {
            'SMS_GATEWAY_URL': 'https://api.sms-gateway.test/v1/send',
            'SMS_GATEWAY_KEY': 'test_dlt_key_123',
            'SMS_SENDER_ID': 'PRLOCK',
            'SMS_DLT_TEMPLATE_ID': '1277179438043729654',
            'SMS_DLT_ENTITY_ID': '1001500000000007890',
        }):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                success = _send_otp_sms('9855551234', '586215', 'PASSWORD_RESET')
                self.assertTrue(success)
                self.assertEqual(len(captured_requests), 1)

                req = captured_requests[0]
                self.assertEqual(req.full_url, 'https://api.sms-gateway.test/v1/send')
                body = json.loads(req.data.decode('utf-8'))

                # Verify exact Jio DLT Content Template text
                self.assertEqual(
                    body['message'],
                    "PRIME LOCKER PRO: Your OTP for verification is 586215. This OTP is valid for 5 minutes. Do not share it with anyone."
                )
                self.assertEqual(body['sender'], 'PRLOCK')
                self.assertEqual(body['template_id'], '1277179438043729654')
                self.assertEqual(body['pe_id'], '1001500000000007890')
                self.assertEqual(body['mobile'], '9855551234')
                self.assertEqual(body['otp'], '586215')

        # 2. Test 5-minute OTP validity window (300 seconds)
        fresh_otp = PasswordResetOTP.objects.create(user=self.retailer, otp='112233', purpose='TEST')
        self.assertTrue(fresh_otp.is_valid())

        # At 4 minutes: still valid
        fresh_otp.created_at = timezone.now() - timedelta(minutes=4)
        fresh_otp.save()
        self.assertTrue(fresh_otp.is_valid())

        # At 6 minutes: expired
        fresh_otp.created_at = timezone.now() - timedelta(minutes=6)
        fresh_otp.save()
        self.assertFalse(fresh_otp.is_valid())


@override_settings(
    DEBUG=True,
    CASHFREE_SIMULATION=True,
    CASHFREE_CONFIG={
        'ENV': 'SANDBOX',
        'APP_ID': '',
        'SECRET_KEY': '',
        'WEBHOOK_SECRET': '',
        'API_VERSION': '2023-08-01',
        'BASE_URL': 'https://sandbox.cashfree.com/pg',
    },
)
class RetailerBankAndMandateTests(TestCase):
    def setUp(self):
        from accounts.models import AuditLog, RetailerBankAccount, RetailerMandateHistory
        self.AuditLog = AuditLog
        self.RetailerBankAccount = RetailerBankAccount
        self.RetailerMandateHistory = RetailerMandateHistory

        self.super_admin = User.objects.create_user(
            username='bank_super_admin',
            email='super_bank@test.com',
            password='Password@123',
            role=Role.SUPER_ADMIN,
            mobile_number='+919900000001',
            is_staff=True,
        )
        self.staff_viewer = User.objects.create_user(
            username='bank_viewer',
            email='viewer_bank@test.com',
            password='Password@123',
            role=Role.STAFF_VIEWER,
            mobile_number='+919900000002',
            is_staff=True,
        )
        self.distributor = User.objects.create_user(
            username='bank_dist',
            email='dist_bank@test.com',
            password='Password@123',
            role=Role.DISTRIBUTOR,
            mobile_number='+919900000003',
            parent=self.super_admin,
        )
        self.other_distributor = User.objects.create_user(
            username='bank_other_dist',
            email='other_dist@test.com',
            password='Password@123',
            role=Role.DISTRIBUTOR,
            mobile_number='+919900000004',
            parent=self.super_admin,
        )
        self.retailer = User.objects.create_user(
            username='bank_retailer_1',
            email='ret1_bank@test.com',
            password='Password@123',
            role=Role.RETAILER,
            first_name='Ramesh',
            last_name='Traders',
            business_name='Ramesh Mobile Store',
            mobile_number='+919855511111',
            parent=self.distributor,
        )
        self.retailer_no_bank = User.objects.create_user(
            username='bank_retailer_2',
            email='ret2_bank@test.com',
            password='Password@123',
            role=Role.RETAILER,
            first_name='Suresh',
            last_name='Point',
            mobile_number='+919855522222',
            parent=self.distributor,
        )
        self.customer = User.objects.create_user(
            username='bank_customer_1',
            email='cust_bank@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919855533333',
            parent=self.retailer,
        )

    def _client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_bank_validation_and_optional_upi(self):
        ret_client = self._client_for(self.retailer)

        # Mismatched confirm_account_number rejected
        bad_confirm = ret_client.post('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Kumar',
            'bank_name': 'HDFC Bank',
            'account_number': '123456789012',
            'confirm_account_number': '123456789999',
            'ifsc_code': 'HDFC0001234',
            'mobile_number': '9855511111',
        }, format='json')
        self.assertEqual(bad_confirm.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('confirm_account_number', bad_confirm.data)

        # Invalid IFSC rejected
        bad_ifsc = ret_client.post('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Kumar',
            'bank_name': 'HDFC Bank',
            'account_number': '123456789012',
            'confirm_account_number': '123456789012',
            'ifsc_code': 'BADIFSC',
            'mobile_number': '9855511111',
        }, format='json')
        self.assertEqual(bad_ifsc.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('ifsc_code', bad_ifsc.data)

        # Invalid UPI rejected
        bad_upi = ret_client.post('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Kumar',
            'bank_name': 'HDFC Bank',
            'account_number': '123456789012',
            'confirm_account_number': '123456789012',
            'ifsc_code': 'HDFC0001234',
            'upi_id': 'invalid_upi',
            'mobile_number': '9855511111',
        }, format='json')
        self.assertEqual(bad_upi.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('upi_id', bad_upi.data)

        # Submitting without optional UPI ID succeeds
        no_upi = ret_client.post('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Kumar',
            'bank_name': 'HDFC Bank',
            'account_number': '123456789012',
            'confirm_account_number': '123456789012',
            'ifsc_code': 'hdfc0001234',
            'mobile_number': '9855511111',
            'verify_now': False,
        }, format='json')
        self.assertEqual(no_upi.status_code, status.HTTP_201_CREATED)
        self.assertEqual(no_upi.data['bank_details']['ifsc_code'], 'HDFC0001234')
        self.assertIsNone(no_upi.data['bank_details']['upi_id'])
        self.assertIsNone(no_upi.data['bank_details']['upi_id_masked'])
        self.assertEqual(no_upi.data['bank_details']['verification_status'], 'PENDING')

    def test_auto_sync_to_admin_and_authorized_access_controls(self):
        ret_client = self._client_for(self.retailer)
        submit_resp = ret_client.post('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Kumar',
            'bank_name': 'State Bank of India',
            'account_number': '50100123456789',
            'confirm_account_number': '50100123456789',
            'ifsc_code': 'SBIN0004321',
            'upi_id': 'ramesh.store@okaxis',
            'mobile_number': '+919855511111',
            'verify_now': True,
        }, format='json')
        self.assertEqual(submit_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(submit_resp.data['bank_details']['verification_status'], 'VERIFIED')
        self.assertEqual(submit_resp.data['bank_details']['account_number_masked'], 'XXXXXXXXXX6789')
        self.assertEqual(submit_resp.data['bank_details']['upi_id_masked'], 'ra**********@okaxis')

        # Auto-synced onto Admin Retailer Profile (masked by default)
        admin_client = self._client_for(self.super_admin)
        profile_resp = admin_client.get(f'/api/retailers/{self.retailer.id}/')
        self.assertEqual(profile_resp.status_code, status.HTTP_200_OK)
        summary = profile_resp.data['bank_mandate_details']
        self.assertEqual(summary['retailer_name'], 'Ramesh Traders')
        self.assertEqual(summary['retailer_code'], f'RET-{self.retailer.id:04d}')
        self.assertEqual(summary['account_holder_name'], 'Ramesh Kumar')
        self.assertEqual(summary['bank_name'], 'State Bank of India')
        self.assertEqual(summary['account_number_masked'], 'XXXXXXXXXX6789')
        self.assertEqual(summary['upi_id_masked'], 'ra**********@okaxis')
        self.assertEqual(summary['ifsc_code'], 'SBIN0004321')
        self.assertEqual(summary['verification_status'], 'VERIFIED')
        self.assertNotIn('account_number', summary)
        self.assertNotIn('upi_id', summary)

        # Unmasked access granted only to non-viewer company admin + audit logged
        unmasked_resp = admin_client.get(f'/api/retailers/{self.retailer.id}/bank-details/?unmasked=true')
        self.assertEqual(unmasked_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(unmasked_resp.data['account_number'], '50100123456789')
        self.assertEqual(unmasked_resp.data['upi_id'], 'ramesh.store@okaxis')
        self.assertTrue(
            self.AuditLog.objects.filter(user=self.super_admin, action='VIEWED_UNMASKED_RETAILER_BANK').exists()
        )

        # Parent distributor can view masked summary, denied unmasked
        dist_client = self._client_for(self.distributor)
        dist_bank = dist_client.get(f'/api/retailers/{self.retailer.id}/bank-details/')
        self.assertEqual(dist_bank.status_code, status.HTTP_200_OK)
        self.assertNotIn('account_number', dist_bank.data)
        self.assertEqual(
            dist_client.get(f'/api/retailers/{self.retailer.id}/bank-details/?unmasked=true').status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Viewer staff can read masked details & history, cannot request unmasked or write verify-bank
        viewer_client = self._client_for(self.staff_viewer)
        self.assertEqual(
            viewer_client.get(f'/api/retailers/{self.retailer.id}/bank-details/').status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            viewer_client.get(f'/api/retailers/{self.retailer.id}/bank-details/?unmasked=true').status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            viewer_client.post(f'/api/retailers/{self.retailer.id}/verify-bank/', {'action': 'VERIFY'}, format='json').status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Unrelated distributor, other retailer, and customer denied access
        other_dist_client = self._client_for(self.other_distributor)
        self.assertIn(
            other_dist_client.get(f'/api/retailers/{self.retailer.id}/bank-details/').status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        other_ret_client = self._client_for(self.retailer_no_bank)
        self.assertIn(
            other_ret_client.get(f'/api/retailers/{self.retailer.id}/bank-details/').status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        cust_client = self._client_for(self.customer)
        self.assertEqual(cust_client.get('/api/retailer/bank-details/').status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            cust_client.post(f'/api/retailers/{self.retailer.id}/verify-bank/', {'action': 'VERIFY'}, format='json').status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_mandate_lifecycle_admin_controls_filtering_and_webhooks(self):
        ret_client = self._client_for(self.retailer)
        admin_client = self._client_for(self.super_admin)

        # 1. Submit bank details (without auto verify) then verify from Admin Controls
        ret_client.post('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Traders',
            'bank_name': 'ICICI Bank',
            'account_number': '001122334455',
            'confirm_account_number': '001122334455',
            'ifsc_code': 'ICIC0000112',
            'upi_id': 'ramesh@icici',
            'mobile_number': '9855511111',
            'verify_now': False,
        }, format='json')

        # Admin rejects then verifies
        fail_resp = admin_client.post(f'/api/retailers/{self.retailer.id}/verify-bank/', {
            'action': 'FAILED',
            'remarks': 'Passbook mismatch',
        }, format='json')
        self.assertEqual(fail_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(fail_resp.data['bank_mandate_details']['verification_status'], 'FAILED')

        verify_resp = admin_client.post(f'/api/retailers/{self.retailer.id}/verify-bank/', {
            'action': 'VERIFY',
            'remarks': 'Matched cancelled cheque',
        }, format='json')
        self.assertEqual(verify_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(verify_resp.data['bank_mandate_details']['verification_status'], 'VERIFIED')

        # Updating core bank account number resets verification back to PENDING
        upd_resp = ret_client.patch('/api/retailer/bank-details/', {
            'account_holder_name': 'Ramesh Traders',
            'bank_name': 'ICICI Bank',
            'account_number': '001122339988',
            'confirm_account_number': '001122339988',
            'ifsc_code': 'ICIC0000112',
            'upi_id': 'ramesh@icici',
            'mobile_number': '9855511111',
            'verify_now': False,
        }, format='json')
        self.assertEqual(upd_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(upd_resp.data['bank_details']['verification_status'], 'PENDING')

        # Retailer triggers self-service verify
        self_verify = ret_client.post('/api/retailer/bank-details/verify/', {}, format='json')
        self.assertEqual(self_verify.status_code, status.HTTP_200_OK)
        self.assertEqual(self_verify.data['verification_status'], 'VERIFIED')

        # 2. Retailer initiates UPI e-Mandate
        mandate_resp = ret_client.post('/api/retailer/upi-mandate/register/', {
            'auth_mode': 'UPI',
            'max_amount': '25000.00',
        }, format='json')
        self.assertEqual(mandate_resp.status_code, status.HTTP_201_CREATED)
        mandate_id = mandate_resp.data['mandate_id']
        self.assertTrue(mandate_id.startswith('ret_sub_sim_'))
        self.assertEqual(mandate_resp.data['upi_mandate_status'], 'PENDING')

        # Before mandate completion, filter by PENDING returns both retailers, COMPLETED returns none
        pending_list = admin_client.get('/api/retailers/?mandate_status=PENDING')
        self.assertEqual(pending_list.status_code, status.HTTP_200_OK)
        pending_ids = [r['id'] for r in pending_list.data.get('results', pending_list.data)]
        self.assertIn(self.retailer.id, pending_ids)
        self.assertIn(self.retailer_no_bank.id, pending_ids)

        completed_before = admin_client.get('/api/retailers/?mandate_status=COMPLETED')
        completed_ids_before = [r['id'] for r in completed_before.data.get('results', completed_before.data)]
        self.assertNotIn(self.retailer.id, completed_ids_before)

        # 3. Admin checks UPI e-Mandate status -> simulation returns ACTIVE -> normalized COMPLETED
        check_resp = admin_client.post(f'/api/retailers/{self.retailer.id}/check-mandate-status/', {}, format='json')
        self.assertEqual(check_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(check_resp.data['synced'])
        self.assertEqual(check_resp.data['mandate_status'], 'COMPLETED')
        self.assertIsNotNone(check_resp.data['bank_mandate_details']['mandate_registration_date'])
        self.assertTrue(check_resp.data['bank_mandate_details']['mandate_reference_id'].startswith('UMRN'))

        # Filter by COMPLETED now includes retailer, PENDING excludes retailer
        completed_after = admin_client.get('/api/retailers/?mandate_status=COMPLETED')
        completed_ids_after = [r['id'] for r in completed_after.data.get('results', completed_after.data)]
        self.assertIn(self.retailer.id, completed_ids_after)

        # Retailer status endpoint reflects the same synced state
        ret_status = ret_client.get('/api/retailer/upi-mandate/status/')
        self.assertEqual(ret_status.status_code, status.HTTP_200_OK)
        self.assertEqual(ret_status.data['mandate_status'], 'COMPLETED')

        # 4. Cashfree Webhook: debit success then mandate failure
        anon_client = APIClient()
        debit_ok = anon_client.post('/api/emi/webhook/cashfree/', {
            'type': 'SUBSCRIPTION_DEBIT_SUCCESS',
            'data': {
                'subscription': {'subscription_id': mandate_id, 'umrn': 'UMRN_CF_9001'},
                'payment': {'cf_payment_id': 'cf_pay_9001', 'payment_amount': '1500.00'},
            },
        }, format='json')
        self.assertEqual(debit_ok.status_code, status.HTTP_200_OK)

        profile_after_debit = admin_client.get(f'/api/retailers/{self.retailer.id}/')
        self.assertEqual(profile_after_debit.data['last_mandate_transaction_status'], 'SUCCESS')
        self.assertEqual(profile_after_debit.data['mandate_reference_id'], 'UMRN_CF_9001')

        mandate_fail = anon_client.post('/api/emi/webhook/cashfree/', {
            'type': 'MANDATE_FAILED',
            'data': {'subscription': {'subscription_id': mandate_id}},
        }, format='json')
        self.assertEqual(mandate_fail.status_code, status.HTTP_200_OK)

        failed_list = admin_client.get('/api/retailers/?mandate_status=FAILED')
        failed_ids = [r['id'] for r in failed_list.data.get('results', failed_list.data)]
        self.assertEqual(failed_ids, [self.retailer.id])

        # 5. Admin views complete Mandate History
        hist_resp = admin_client.get(f'/api/retailers/{self.retailer.id}/mandate-history/')
        self.assertEqual(hist_resp.status_code, status.HTTP_200_OK)
        event_types = [h['event_type'] for h in hist_resp.data['history']]
        self.assertIn('BANK_DETAILS_SUBMITTED', event_types)
        self.assertIn('ADMIN_BANK_VERIFY', event_types)
        self.assertIn('MANDATE_INITIATED', event_types)
        self.assertIn('MANDATE_STATUS_CHECKED', event_types)
        self.assertIn('SUBSCRIPTION_DEBIT_SUCCESS', event_types)
        self.assertIn('MANDATE_FAILED', event_types)


from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from keys.models import DeviceKey, KeyStatus, KeyRequest, KeyRequestStatus

User = get_user_model()

class KeysManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='key_superadmin',
            email='admin@test.com',
            password='AdminPassword@123',
            role='SUPER_ADMIN'
        )
        self.retailer = User.objects.create_user(
            username='key_retailer',
            email='retailer@test.com',
            password='RetailerPassword@123',
            role='RETAILER',
            parent=self.admin
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(user=self.admin)
        self.ret_client = APIClient()
        self.ret_client.force_authenticate(user=self.retailer)

    def test_generate_dlc_keys(self):
        resp = self.admin_client.post('/api/keys/generate/', {
            'prefix': 'DLC',
            'device_type': 'DLC',
            'quantity': 5
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        
        dlc_keys = DeviceKey.objects.filter(current_owner=self.admin, device_type='DLC')
        self.assertEqual(dlc_keys.count(), 5)
        self.assertTrue(all(k.key_string.startswith('DLC-') for k in dlc_keys))

    def test_keys_dashboard_and_breakdown(self):
        # Generate DLC and ANDROID keys
        self.admin_client.post('/api/keys/generate/', {'prefix': 'DLC', 'device_type': 'DLC', 'quantity': 3}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'AND', 'device_type': 'ANDROID', 'quantity': 2}, format='json')

        dash_resp = self.admin_client.get('/api/keys/dashboard/')
        self.assertEqual(dash_resp.status_code, status.HTTP_200_OK)
        data = dash_resp.data
        self.assertEqual(data['dlc_keys_left'], 3)
        self.assertEqual(data['android_keys_left'], 2)
        self.assertEqual(data['total_keys_left'], 5)

    def test_allocate_dlc_keys_to_retailer(self):
        self.admin_client.post('/api/keys/generate/', {'prefix': 'DLC', 'device_type': 'DLC', 'quantity': 4}, format='json')
        alloc_resp = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.retailer.id,
            'device_type': 'DLC',
            'quantity': 2
        }, format='json')
        self.assertEqual(alloc_resp.status_code, status.HTTP_200_OK)
        
        ret_dash = self.ret_client.get('/api/keys/dashboard/')
        self.assertEqual(ret_dash.data['dlc_keys_left'], 2)

    def test_key_request_dlc_keys(self):
        self.admin_client.post('/api/keys/generate/', {'prefix': 'DLC', 'device_type': 'DLC', 'quantity': 5}, format='json')
        
        # Retailer requests 2 DLC keys
        req_resp = self.ret_client.post('/api/key-requests/', {
            'quantity': 2,
            'device_type': 'DLC'
        }, format='json')
        self.assertEqual(req_resp.status_code, status.HTTP_201_CREATED)
        req_id = req_resp.data['id']

        # Admin approves
        appr_resp = self.admin_client.post(f'/api/key-requests/{req_id}/approve/', {}, format='json')
        self.assertEqual(appr_resp.status_code, status.HTTP_200_OK)

        ret_dash = self.ret_client.get('/api/keys/dashboard/')
        self.assertEqual(ret_dash.data['dlc_keys_left'], 2)


class KeyServiceBalanceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='svc_key_admin',
            email='svcadmin@test.com',
            password='AdminPassword@123',
            role='SUPER_ADMIN'
        )
        self.retailer = User.objects.create_user(
            username='svc_key_retailer',
            email='svcretailer@test.com',
            password='RetailerPassword@123',
            role='RETAILER',
            parent=self.admin
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(user=self.admin)
        self.ret_client = APIClient()
        self.ret_client.force_authenticate(user=self.retailer)

    def test_check_balance_endpoint_all_services(self):
        from keys.services import ZERO_KEY_MESSAGE

        for service_param, expected_label in [
            ('ANDROID_MDM', 'Android MDM'),
            ('ANDROID_DLC', 'Android DLC Locker'),
            ('IOS_LOCKER', 'iOS Locker'),
        ]:
            resp = self.ret_client.get(f'/api/keys/check-balance/?service={service_param}')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data['service'], expected_label)
            self.assertEqual(resp.data['available_balance'], 0)
            self.assertFalse(resp.data['can_enroll'])
            self.assertEqual(resp.data['message'], ZERO_KEY_MESSAGE)

        # Allocate keys across all 3 services and re-verify
        self.admin_client.post('/api/keys/generate/', {'prefix': 'AND', 'device_type': 'ANDROID', 'quantity': 1}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'DLC', 'device_type': 'DLC', 'quantity': 2}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'IOS', 'device_type': 'IOS', 'quantity': 1}, format='json')

        self.admin_client.post('/api/keys/allocate/', {'target_user_id': self.retailer.id, 'device_type': 'ANDROID', 'quantity': 1}, format='json')
        self.admin_client.post('/api/keys/allocate/', {'target_user_id': self.retailer.id, 'device_type': 'DLC', 'quantity': 2}, format='json')
        self.admin_client.post('/api/keys/allocate/', {'target_user_id': self.retailer.id, 'device_type': 'IOS', 'quantity': 1}, format='json')

        resp_mdm = self.ret_client.get('/api/keys/check-balance/?service=ANDROID_MDM')
        self.assertEqual(resp_mdm.data['available_balance'], 1)
        self.assertTrue(resp_mdm.data['can_enroll'])
        self.assertIsNone(resp_mdm.data['message'])

        resp_dlc = self.ret_client.get('/api/keys/check-balance/?service=ANDROID_DLC')
        self.assertEqual(resp_dlc.data['available_balance'], 2)
        self.assertTrue(resp_dlc.data['can_enroll'])

        resp_ios = self.ret_client.get('/api/keys/check-balance/?service=IOS_LOCKER')
        self.assertEqual(resp_ios.data['available_balance'], 1)
        self.assertTrue(resp_ios.data['can_enroll'])

    def test_key_request_and_approve_for_all_services(self):
        self.admin_client.post('/api/keys/generate/', {'prefix': 'AND', 'device_type': 'ANDROID', 'quantity': 2}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'DLC', 'device_type': 'DLC', 'quantity': 2}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'IOS', 'device_type': 'IOS', 'quantity': 2}, format='json')

        for service_code in ['ANDROID_MDM', 'ANDROID_DLC', 'IOS_LOCKER']:
            req_resp = self.ret_client.post('/api/key-requests/', {
                'service': service_code,
                'quantity': 1,
                'notes': f'Need {service_code} key'
            }, format='json')
            self.assertEqual(req_resp.status_code, status.HTTP_201_CREATED)
            req_id = req_resp.data['id']

            appr_resp = self.admin_client.post(f'/api/key-requests/{req_id}/approve/', {}, format='json')
            self.assertEqual(appr_resp.status_code, status.HTTP_200_OK)

            bal_resp = self.ret_client.get(f'/api/keys/check-balance/?service={service_code}')
            self.assertEqual(bal_resp.data['available_balance'], 1)
            self.assertTrue(bal_resp.data['can_enroll'])

    def test_allocate_mobile_family_and_service_aliases(self):
        # Generate ANDROID, DLC, IOS, and TV keys
        self.admin_client.post('/api/keys/generate/', {'prefix': 'AND', 'device_type': 'ANDROID', 'quantity': 5}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'DLC', 'device_type': 'DLC', 'quantity': 5}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'IOS', 'device_type': 'IOS', 'quantity': 5}, format='json')
        self.admin_client.post('/api/keys/generate/', {'prefix': 'TV', 'device_type': 'TV', 'quantity': 5}, format='json')

        # 1. Allocate using device_type='MOBILE' -> should transfer Android/iOS mobile keys
        alloc_mobile = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.retailer.id,
            'device_type': 'MOBILE',
            'quantity': 2,
        }, format='json')
        self.assertEqual(alloc_mobile.status_code, status.HTTP_200_OK)
        self.assertGreater(alloc_mobile.data['your_remaining_available_keys'], 0)

        # 2. Allocate using alias 'ANDROID_MDM'
        alloc_and = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.retailer.id,
            'device_type': 'ANDROID_MDM',
            'quantity': 1,
        }, format='json')
        self.assertEqual(alloc_and.status_code, status.HTTP_200_OK)

        # 3. Allocate using alias 'ANDROID_DLC'
        alloc_dlc = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.retailer.id,
            'device_type': 'ANDROID_DLC',
            'quantity': 2,
        }, format='json')
        self.assertEqual(alloc_dlc.status_code, status.HTTP_200_OK)

        # 4. Allocate using alias 'IOS_LOCKER'
        alloc_ios = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.retailer.id,
            'device_type': 'IOS_LOCKER',
            'quantity': 2,
        }, format='json')
        self.assertEqual(alloc_ios.status_code, status.HTTP_200_OK)

        # 5. Allocate using alias 'SMART_TV'
        alloc_tv = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.retailer.id,
            'device_type': 'SMART_TV',
            'quantity': 2,
        }, format='json')
        self.assertEqual(alloc_tv.status_code, status.HTTP_200_OK)

        # 6. Reclaim using device_type='MOBILE'
        reclaim_mobile = self.admin_client.post('/api/keys/reclaim/', {
            'target_user_id': self.retailer.id,
            'device_type': 'MOBILE',
            'quantity': 1,
        }, format='json')
        self.assertEqual(reclaim_mobile.status_code, status.HTTP_200_OK)
        self.assertGreater(reclaim_mobile.data['your_total_available_keys'], 0)


class KeyTransactionGroupedCountTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='grp_admin',
            email='grp_admin@test.com',
            password='AdminPassword@123',
            role='SUPER_ADMIN'
        )
        self.distributor = User.objects.create_user(
            username='grp_distributor',
            email='dist@test.com',
            password='DistPassword@123',
            role='DISTRIBUTOR',
            parent=self.admin
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(user=self.admin)

    def test_grouped_transactions_show_whole_count(self):
        # 1. Admin generates 10 DLC keys
        gen_resp = self.admin_client.post('/api/keys/generate/', {
            'prefix': 'DLC',
            'device_type': 'DLC',
            'quantity': 10
        }, format='json')
        self.assertEqual(gen_resp.status_code, status.HTTP_201_CREATED)

        # 2. Admin allocates 5 DLC keys to distributor
        alloc_resp = self.admin_client.post('/api/keys/allocate/', {
            'target_user_id': self.distributor.id,
            'device_type': 'DLC',
            'quantity': 5,
            'reference_note': 'Test batch allocation of 5 keys'
        }, format='json')
        self.assertEqual(alloc_resp.status_code, status.HTTP_200_OK)

        # 3. Query transactions default view (grouped whole count)
        tx_resp = self.admin_client.get('/api/keys/transactions/')
        self.assertEqual(tx_resp.status_code, status.HTTP_200_OK)
        
        # Results should be grouped batches, not 15 individual rows!
        results = tx_resp.data.get('results', tx_resp.data)
        self.assertEqual(len(results), 2)  # Exactly 2 batches: 1 ALLOCATED, 1 GENERATED

        # Check ALLOCATED batch (most recent)
        allocated_batch = results[0]
        self.assertEqual(allocated_batch['action'], 'ALLOCATED')
        self.assertEqual(allocated_batch['quantity'], 5)
        self.assertIn('(+4 more)', allocated_batch['key_string'])
        self.assertEqual(len(allocated_batch['keys']), 5)
        self.assertEqual(allocated_batch['to_user'], self.distributor.id)

        # Check GENERATED batch
        generated_batch = results[1]
        self.assertEqual(generated_batch['action'], 'GENERATED')
        self.assertEqual(generated_batch['quantity'], 10)
        self.assertIn('(+9 more)', generated_batch['key_string'])
        self.assertEqual(len(generated_batch['keys']), 10)

        # 4. Query detailed/raw view (should return individual 15 rows)
        detailed_resp = self.admin_client.get('/api/keys/transactions/?view=detailed')
        self.assertEqual(detailed_resp.status_code, status.HTTP_200_OK)
        detailed_results = detailed_resp.data.get('results', detailed_resp.data)
        self.assertEqual(len(detailed_results), 15)  # 5 allocated + 10 generated
        self.assertEqual(detailed_results[0]['quantity'], 1)

        # 5. CSV export in grouped mode
        csv_resp = self.admin_client.get('/api/keys/transactions/?export=csv')
        self.assertEqual(csv_resp.status_code, status.HTTP_200_OK)
        self.assertIn(b'Quantity', csv_resp.content)


class KeyBreakdownAndCustomerExclusionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.super_admin = User.objects.create_superuser(
            username='admin_bk',
            email='admin_bk@test.com',
            password='password123',
            role='SUPER_ADMIN'
        )
        self.distributor = User.objects.create_user(
            username='dist_bk',
            email='dist_bk@test.com',
            password='password123',
            role='DISTRIBUTOR',
            parent=self.super_admin
        )
        self.retailer = User.objects.create_user(
            username='ret_bk',
            email='ret_bk@test.com',
            password='password123',
            role='RETAILER',
            parent=self.distributor
        )
        self.customer = User.objects.create_user(
            username='cust_bk',
            email='cust_bk@test.com',
            password='password123',
            role='CUSTOMER',
            parent=self.retailer
        )

        # Create available keys of different types for retailer
        DeviceKey.objects.create(key_string='AND-11111111', device_type='ANDROID', status=KeyStatus.AVAILABLE, current_owner=self.retailer)
        DeviceKey.objects.create(key_string='AND-22222222', device_type='ANDROID', status=KeyStatus.AVAILABLE, current_owner=self.retailer)
        DeviceKey.objects.create(key_string='IOS-33333333', device_type='IOS', status=KeyStatus.AVAILABLE, current_owner=self.retailer)
        DeviceKey.objects.create(key_string='DLC-44444444', device_type='DLC', status=KeyStatus.AVAILABLE, current_owner=self.retailer)
        DeviceKey.objects.create(key_string='DLC-55555555', device_type='DLC', status=KeyStatus.AVAILABLE, current_owner=self.retailer)
        DeviceKey.objects.create(key_string='DLC-66666666', device_type='DLC', status=KeyStatus.AVAILABLE, current_owner=self.retailer)
        DeviceKey.objects.create(key_string='TV-77777777', device_type='TV', status=KeyStatus.AVAILABLE, current_owner=self.retailer)

        # An activated key that should NOT be in available counts
        DeviceKey.objects.create(key_string='AND-99999999', device_type='ANDROID', status=KeyStatus.ACTIVATED, current_owner=self.retailer)

    def test_get_user_available_keys_breakdown_function(self):
        from keys.services import get_user_available_keys_breakdown
        
        # Retailer check
        breakdown = get_user_available_keys_breakdown(self.retailer)
        self.assertIsNotNone(breakdown)
        self.assertEqual(breakdown['total'], 7)
        self.assertEqual(breakdown['mdm_keys'], 2)
        self.assertEqual(breakdown['ios_keys'], 1)
        self.assertEqual(breakdown['dlc_keys'], 3)
        self.assertEqual(breakdown['tv_keys'], 1)

        # Customer check (MUST return None)
        cust_breakdown = get_user_available_keys_breakdown(self.customer)
        self.assertIsNone(cust_breakdown)

    def test_auth_me_available_keys_for_retailer_and_customer(self):
        # 1. Retailer
        self.client.force_authenticate(user=self.retailer)
        ret_resp = self.client.get('/api/auth/me/')
        self.assertEqual(ret_resp.status_code, status.HTTP_200_OK)
        avail = ret_resp.data.get('available_keys')
        self.assertIsNotNone(avail)
        self.assertEqual(avail['total'], 7)
        self.assertEqual(avail['mdm_keys'], 2)
        self.assertEqual(avail['ios_keys'], 1)
        self.assertEqual(avail['dlc_keys'], 3)
        self.assertEqual(avail['tv_keys'], 1)

        # 2. Customer
        self.client.force_authenticate(user=self.customer)
        cust_resp = self.client.get('/api/auth/me/')
        self.assertEqual(cust_resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(cust_resp.data.get('available_keys'))

    def test_serializers_available_keys(self):
        from accounts.serializers import RetailerSerializer, UserSerializer, CustomerSerializer

        # Retailer Serializer
        ret_data = RetailerSerializer(self.retailer).data
        self.assertIsNotNone(ret_data.get('available_keys'))
        self.assertEqual(ret_data['available_keys']['mdm_keys'], 2)
        self.assertEqual(ret_data['available_keys']['ios_keys'], 1)
        self.assertEqual(ret_data['available_keys']['dlc_keys'], 3)
        self.assertEqual(ret_data['available_keys']['tv_keys'], 1)
        self.assertEqual(ret_data['available_keys']['total'], 7)

        # User Serializer for customer
        user_cust_data = UserSerializer(self.customer).data
        self.assertIsNone(user_cust_data.get('available_keys'))

        # Customer Serializer
        cust_data = CustomerSerializer(self.customer).data
        self.assertNotIn('available_keys', cust_data)

    def test_keys_check_balance_and_dashboard_endpoints(self):
        self.client.force_authenticate(user=self.retailer)
        
        # Check Balance endpoint
        bal_resp = self.client.get('/api/keys/check-balance/')
        self.assertEqual(bal_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(bal_resp.data['total_available'], 7)
        self.assertEqual(bal_resp.data['mdm_keys'], 2)
        self.assertEqual(bal_resp.data['ios_keys'], 1)
        self.assertEqual(bal_resp.data['dlc_keys'], 3)
        self.assertEqual(bal_resp.data['tv_keys'], 1)
        self.assertEqual(bal_resp.data['available_keys']['total'], 7)

        # Dashboard endpoint
        dash_resp = self.client.get('/api/keys/dashboard/')
        self.assertEqual(dash_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(dash_resp.data['total_keys_left'], 7)
        self.assertEqual(dash_resp.data['mdm_keys_left'], 2)
        self.assertEqual(dash_resp.data['ios_keys_left'], 1)
        self.assertEqual(dash_resp.data['dlc_keys_left'], 3)
        self.assertEqual(dash_resp.data['tv_keys_left'], 1)



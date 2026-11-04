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

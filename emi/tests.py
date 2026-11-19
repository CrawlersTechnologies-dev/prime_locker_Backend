from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.management import call_command
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import Role, PlatformSettings
from devices.models import (
    Device,
    DeviceLockStatus,
    DeviceCommand,
    CommandType,
    CommandStatus,
    LockProvider,
    OSType
)
from emi.models import (
    CustomerEMIContract,
    EMIInstallment,
    InstallmentStatus,
    EMIContractStatus,
    PaymentMode
)

User = get_user_model()


@override_settings(ROCKETPAY_SIMULATION=True, CASHFREE_SIMULATION=True)
class EMIBackendTests(TestCase):
    def setUp(self):
        # Configure simulation settings
        settings = PlatformSettings.get_settings()
        settings.auto_lock_enabled = True
        settings.default_grace_period_days = 3
        settings.late_fee_per_day = Decimal('25.00')
        settings.save()

        # Users
        self.retailer = User.objects.create_user(
            username='test_retailer_emi',
            email='retailer_emi@test.com',
            password='Password@123',
            role=Role.RETAILER,
            mobile_number='+919811111111',
            business_name='Test Retailer Store'
        )
        self.customer = User.objects.create_user(
            username='test_customer_emi',
            email='customer_emi@test.com',
            password='Password@123',
            role=Role.CUSTOMER,
            mobile_number='+919822222222',
            parent=self.retailer
        )

        # Device
        self.device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671069999001',
            brand='VIVO',
            model='Y35',
            status=DeviceLockStatus.LOCKED,
            lock_provider=LockProvider.OWN_AGENT
        )

        # EMI Contract
        self.contract = CustomerEMIContract.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            device_imei=self.device.imei,
            device_name='Vivo Y35',
            total_amount=Decimal('12000.00'),
            financed_amount=Decimal('12000.00'),
            monthly_emi_amount=Decimal('1000.00'),
            first_emi_date=timezone.localdate() - timedelta(days=60),
            tenure_months=12,
            grace_period_days=3,
            status=EMIContractStatus.LOCKED
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.retailer)

    def test_auto_lock_overdue_command(self):
        """Tests that auto_lock_overdue locks devices past grace period"""
        # Set device to ACTIVE initially
        self.device.status = DeviceLockStatus.ACTIVE
        self.device.save()
        self.contract.status = EMIContractStatus.ACTIVE
        self.contract.save()

        # Create an installment 10 days overdue (grace period is 3 days)
        due_date = timezone.localdate() - timedelta(days=10)
        installment = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=due_date,
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )

        # Execute auto-lock command
        call_command('auto_lock_overdue')

        # Verify contract and device were locked
        self.contract.refresh_from_db()
        self.device.refresh_from_db()
        installment.refresh_from_db()

        self.assertEqual(installment.status, InstallmentStatus.OVERDUE)
        self.assertEqual(self.contract.status, EMIContractStatus.LOCKED)
        self.assertEqual(self.device.status, DeviceLockStatus.LOCKED)

        # Verify DeviceCommand was created
        cmd = DeviceCommand.objects.filter(device=self.device, command_type=CommandType.LOCK).first()
        self.assertIsNotNone(cmd)

    def test_unlock_after_payment_clears_overdue(self):
        """Tests that recording payment auto-unlocks device when no overdue installments remain"""
        due_date = timezone.localdate() - timedelta(days=5)
        installment = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=due_date,
            amount=Decimal('1000.00'),
            status=InstallmentStatus.OVERDUE
        )
        # Create second upcoming installment for next month so contract stays ACTIVE
        EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=2,
            due_date=timezone.localdate() + timedelta(days=25),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )

        # Device is currently LOCKED
        self.assertEqual(self.device.status, DeviceLockStatus.LOCKED)

        # Record payment for the overdue installment
        url = f'/api/emi-installments/{installment.id}/record-payment/'
        payload = {
            'amount': '1000.00',
            'payment_mode': PaymentMode.CASH,
            'collection_date': timezone.now().isoformat()
        }
        resp = self.client.post(url, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.device.refresh_from_db()
        self.contract.refresh_from_db()
        installment.refresh_from_db()

        self.assertEqual(installment.status, InstallmentStatus.PAID)
        self.assertEqual(self.device.status, DeviceLockStatus.ACTIVE)
        self.assertEqual(self.contract.status, EMIContractStatus.ACTIVE)

        # Verify UNLOCK command was queued
        unlock_cmd = DeviceCommand.objects.filter(device=self.device, command_type=CommandType.UNLOCK).first()
        self.assertIsNotNone(unlock_cmd)

    def test_tv_contract_collateral_swap_refuses_iphone(self):
        """Tests TV EMI collateral phone swap, allowing Android but refusing iPhone"""
        # Create second Android phone
        android_collateral = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='866671069999002',
            brand='SAMSUNG',
            model='Galaxy M34',
            os_type=OSType.ANDROID,
            status=DeviceLockStatus.ACTIVE
        )

        # Create iPhone
        iphone_device = Device.objects.create(
            customer=self.customer,
            retailer=self.retailer,
            imei='354928109999003',
            brand='Apple',
            model='iPhone 14',
            os_type=OSType.IOS,
            status=DeviceLockStatus.ACTIVE
        )

        # Swap to Android phone should succeed
        swap_url = f'/api/emi-contracts/{self.contract.id}/swap-lock-device/'
        resp = self.client.post(swap_url, {'imei': android_collateral.imei}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.lock_device, android_collateral)

        # Swap to iPhone should be refused with HTTP 400
        resp_iphone = self.client.post(swap_url, {'imei': iphone_device.imei}, format='json')
        self.assertEqual(resp_iphone.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('iPhones cannot be used as collateral', resp_iphone.data.get('error', ''))

    def test_completed_contract_unregisters_rocketpay_key(self):
        """Tests RP10: When all installments are paid, contract completes and unregisters RocketPay key"""
        # Configure device to ROCKETPAY provider
        self.device.lock_provider = LockProvider.ROCKETPAY
        self.device.provider_device_id = 'rp_test_close_123'
        self.device.save()

        # Delete any existing installments and create exactly 1 installment
        self.contract.installments.all().delete()
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=timezone.localdate(),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )

        # Pay this installment in full
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                f'/api/emi-installments/{inst.id}/record-payment/',
                {'amount': '1000.00', 'payment_mode': PaymentMode.CASH},
                format='json'
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, EMIContractStatus.COMPLETED)

        self.device.refresh_from_db()
        self.assertEqual(self.device.provider_status, 'UNREGISTERED')

    def test_cashfree_create_payment_order(self):
        """Tests creating a Cashfree payment order returning payment_session_id"""
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=timezone.localdate(),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )
        url = f'/api/emi-installments/{inst.id}/create-payment-order/'
        with override_settings(CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': ''}):
            resp = self.client.post(url, {}, format='json')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data.get('gateway'), 'CASHFREE')
            self.assertIn('payment_session_id', resp.data)
            self.assertIn('order_id', resp.data)
            self.assertEqual(resp.data.get('order_amount'), '1000.00')

    def test_cashfree_verify_online_payment(self):
        """Tests online payment verification using Cashfree order_id in simulation mode"""
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=timezone.localdate(),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )
        url = f'/api/emi-installments/{inst.id}/verify-online-payment/'
        payload = {'order_id': f'cf_sim_{inst.id}'}
        with override_settings(CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': ''}):
            resp = self.client.post(url, payload, format='json')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

            inst.refresh_from_db()
            self.assertEqual(inst.status, InstallmentStatus.PAID)
            self.assertEqual(inst.paid_amount, Decimal('1000.00'))

    def test_cashfree_webhook_payment_success(self):
        """Tests Cashfree webhook asynchronous order fulfillment"""
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=timezone.localdate(),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )
        webhook_url = '/api/emi/webhook/cashfree/'
        payload = {
            "type": "PAYMENT_SUCCESS_WEBHOOK",
            "event_time": timezone.now().isoformat(),
            "data": {
                "order": {
                    "order_id": f"cf_inst_{inst.id}_test123",
                    "order_amount": 1000.0,
                    "order_currency": "INR"
                },
                "payment": {
                    "cf_payment_id": "cf_pay_998877",
                    "payment_status": "SUCCESS",
                    "payment_amount": 1000.0
                }
            }
        }
        import json
        import hmac
        import hashlib
        import base64

        secret = 'test_webhook_sec'
        timestamp = '1690000000'
        body_bytes = json.dumps(payload).encode('utf-8')
        ts_payload = timestamp.encode('utf-8') + body_bytes
        valid_sig = base64.b64encode(hmac.new(secret.encode('utf-8'), ts_payload, hashlib.sha256).digest()).decode('utf-8')

        with override_settings(CASHFREE_CONFIG={'APP_ID': 'test_app', 'SECRET_KEY': secret, 'ENV': 'TEST', 'API_VERSION': '2023-08-01'}):
            resp = self.client.post(
                webhook_url,
                data=body_bytes,
                content_type='application/json',
                HTTP_X_WEBHOOK_SIGNATURE=valid_sig,
                HTTP_X_WEBHOOK_TIMESTAMP=timestamp
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

        inst.refresh_from_db()
        self.assertEqual(inst.status, InstallmentStatus.PAID)
        self.assertEqual(inst.transaction_ref, 'cf_pay_998877')

    def test_cashfree_webhook_signature_verification(self):
        """Tests webhook HMAC-SHA256 signature verification with live secret configured"""
        import hmac
        import hashlib
        import base64
        from emi.services.cashfree_service import CashfreeService

        secret = 'test_cashfree_secret_key_12345'
        timestamp = '1690000000'
        raw_body = b'{"type": "TEST_WEBHOOK"}'

        # Compute valid signature
        ts_payload = timestamp.encode('utf-8') + raw_body
        valid_sig = base64.b64encode(hmac.new(secret.encode('utf-8'), ts_payload, hashlib.sha256).digest()).decode('utf-8')

        with override_settings(CASHFREE_CONFIG={'APP_ID': 'test_app', 'SECRET_KEY': secret, 'ENV': 'TEST', 'API_VERSION': '2023-08-01'}):
            # Valid signature check
            self.assertTrue(CashfreeService.verify_webhook_signature(raw_body, timestamp, valid_sig))
            # Invalid signature check
            self.assertFalse(CashfreeService.verify_webhook_signature(raw_body, timestamp, 'invalid_sig'))

    def test_online_payment_simulation_refused_when_debug_false(self):
        """Tests test-mode security hole fix: simulated payment verification is refused when DEBUG=False"""
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=timezone.localdate(),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )
        url = f'/api/emi-installments/{inst.id}/verify-online-payment/'
        payload = {'order_id': 'cf_sim_fake_hack'}

        # With DEBUG=False and no simulation override
        with override_settings(DEBUG=False, CASHFREE_SIMULATION=False, CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': '', 'ENV': 'PROD'}):
            resp = self.client.post(url, payload, format='json')
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

            inst.refresh_from_db()
            self.assertNotEqual(inst.status, InstallmentStatus.PAID)
            self.assertEqual(inst.status, InstallmentStatus.UPCOMING)

    def test_simulated_order_id_refused_when_credentials_active(self):
        """Tests that even with DEBUG=True, cf_sim_ order IDs are refused when Cashfree credentials exist"""
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=timezone.localdate(),
            amount=Decimal('1000.00'),
            status=InstallmentStatus.UPCOMING
        )
        url = f'/api/emi-installments/{inst.id}/verify-online-payment/'
        with override_settings(DEBUG=True, CASHFREE_CONFIG={'APP_ID': 'live_app', 'SECRET_KEY': 'live_sec', 'ENV': 'TEST'}):
            resp = self.client.post(url, {'order_id': 'cf_sim_hack_attempt'}, format='json')
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn('not permitted when Cashfree credentials are configured', str(resp.data))

    def test_late_fee_charged_in_online_payment_order(self):
        """Tests that create_payment_order and payment verification charge total_due (amount + late fees)"""
        # Create an installment 10 days overdue
        # Grace period is 3 days, late_fee_per_day is ₹25, so 7 chargeable days = ₹175 late fee
        due_date = timezone.localdate() - timedelta(days=10)
        inst = EMIInstallment.objects.create(
            contract=self.contract,
            installment_number=1,
            due_date=due_date,
            amount=Decimal('1000.00'),
            status=InstallmentStatus.OVERDUE
        )

        expected_late_fee = Decimal('175.00')
        expected_total = Decimal('1175.00')
        self.assertEqual(inst.late_fee, expected_late_fee)
        self.assertEqual(inst.total_due, expected_total)

        # 1. Create order: Cashfree should charge total_due (₹1,175), not base amount (₹1,000)
        create_url = f'/api/emi-installments/{inst.id}/create-payment-order/'
        with override_settings(CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': ''}):
            resp_create = self.client.post(create_url, {}, format='json')
            self.assertEqual(resp_create.status_code, status.HTTP_200_OK)
            self.assertEqual(resp_create.data.get('order_amount'), '1175.00')

        # 2. Verify payment: records paid_amount as total_due (₹1,175)
        verify_url = f'/api/emi-installments/{inst.id}/verify-online-payment/'
        with override_settings(CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': ''}):
            resp_verify = self.client.post(verify_url, {'order_id': f'cf_sim_{inst.id}'}, format='json')
            self.assertEqual(resp_verify.status_code, status.HTTP_200_OK)

            inst.refresh_from_db()
            self.assertEqual(inst.status, InstallmentStatus.PAID)
            self.assertEqual(inst.paid_amount, expected_total)

    def test_cashfree_create_upi_mandate(self):
        create_mandate_url = f'/api/emi-contracts/{self.contract.id}/create-mandate/'
        with override_settings(CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': ''}):
            resp = self.client.post(create_mandate_url, {'auth_mode': 'UPI'}, format='json')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            self.assertTrue(resp.data['success'])
            self.assertEqual(resp.data['mandate_status'], 'PENDING_AUTH')
            self.assertIn('mandate_auth_url', resp.data)

            self.contract.refresh_from_db()
            self.assertEqual(self.contract.mandate_status, 'PENDING_AUTH')
            self.assertTrue(bool(self.contract.mandate_id))

    def test_cashfree_mandate_webhook_and_status(self):
        self.contract.mandate_id = 'cf_mandate_webhook_test_123'
        self.contract.mandate_status = 'PENDING_AUTH'
        self.contract.save()

        import json
        webhook_url = '/api/emi/webhook/cashfree/'
        payload = {
            'type': 'MANDATE_SUCCESS',
            'data': {
                'subscription': {
                    'subscription_id': 'cf_mandate_webhook_test_123',
                    'umrn': 'UMRNTEST998877'
                }
            }
        }
        with override_settings(CASHFREE_CONFIG={'APP_ID': '', 'SECRET_KEY': ''}):
            resp = self.client.post(
                webhook_url,
                data=json.dumps(payload),
                content_type='application/json',
                HTTP_X_WEBHOOK_SIGNATURE='valid',
                HTTP_X_WEBHOOK_TIMESTAMP='123456'
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

            self.contract.refresh_from_db()
            self.assertEqual(self.contract.mandate_status, 'ACTIVE')
            self.assertEqual(self.contract.mandate_umrn, 'UMRNTEST998877')

    def test_cashfree_mandate_checkout_view_and_simulation(self):
        mandate_id = f"sub_sim_{self.contract.id}_abc123"
        self.contract.mandate_id = mandate_id
        self.contract.mandate_status = 'PENDING_AUTH'
        self.contract.mandate_auth_url = None
        self.contract.save()

        # 1. Serializer resolves non-null checkout URL even when mandate_auth_url was None in DB
        contract_url = f'/api/emi-contracts/{self.contract.id}/'
        resp = self.client.get(contract_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(resp.data.get('mandate_auth_url'))
        self.assertIn(f'/api/emi/mandate/checkout/{mandate_id}/', resp.data['mandate_auth_url'])

        # 2. Checkout view renders HTML page with simulation mode
        checkout_url = f'/api/emi/mandate/checkout/{mandate_id}/'
        resp = self.client.get(checkout_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/html', resp['Content-Type'])
        self.assertIn('Prime Locker', resp.content.decode('utf-8'))
        self.assertIn('Simulation Test Mode', resp.content.decode('utf-8'))

        # 3. Simulate customer approval via POST
        post_resp = self.client.post(checkout_url, {'simulate_approve': '1'})
        self.assertEqual(post_resp.status_code, 200)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.mandate_status, 'ACTIVE')
        self.assertTrue(bool(self.contract.mandate_umrn))

        # 4. Status query auto-populates absolute mandate_auth_url
        status_url = f'/api/emi-contracts/{self.contract.id}/mandate-status/'
        stat_resp = self.client.get(status_url)
        self.assertEqual(stat_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(stat_resp.data['mandate_status'], 'ACTIVE')
        self.assertIsNotNone(stat_resp.data.get('mandate_auth_url'))
        self.assertTrue(stat_resp.data['mandate_auth_url'].startswith(('http://', 'https://')))

    def test_cashfree_mandate_checkout_view_sdk_rendering(self):
        mandate_id = f"sub_{self.contract.id}_real999"
        self.contract.mandate_id = mandate_id
        self.contract.mandate_status = 'PENDING_AUTH'
        self.contract.save()

        checkout_url = f'/api/emi/mandate/checkout/{mandate_id}/?session_id=sub_session_real_mock_123'
        with override_settings(CASHFREE_CONFIG={'APP_ID': 'test_app', 'SECRET_KEY': 'test_sec', 'ENV': 'TEST'}):
            resp = self.client.get(checkout_url)
            self.assertEqual(resp.status_code, 200)
            content = resp.content.decode('utf-8')
            self.assertIn('https://sdk.cashfree.com/js/v3/cashfree.js', content)
            self.assertIn('sub_session_real_mock_123', content)
            self.assertIn('Open UPI AutoPay', content)

    def test_auto_generated_agreement_html_and_signature_rendering(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        # Attach digital signature to customer
        fake_sig = SimpleUploadedFile("sig.png", b"fake_png_signature_bytes", content_type="image/png")
        self.customer.customer_signature = fake_sig
        self.customer.first_name = "Ramesh"
        self.customer.last_name = "Kumar"
        self.customer.id_type = "Aadhaar"
        self.customer.id_number = "9876-5432-1098"
        self.customer.save()

        self.retailer.business_name = "Ramesh Electronics & Mobiles"
        self.retailer.first_name = "Suresh"
        self.retailer.last_name = "Patel"
        self.retailer.save()

        # Authenticate as retailer
        self.client.force_authenticate(user=self.retailer)
        url = f'/api/emi-contracts/{self.contract.id}/agreement/'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        html = resp.content.decode('utf-8')
        # Check dynamic customer & retailer fields
        self.assertIn("Ramesh Electronics", html)
        self.assertIn("Ramesh Kumar", html)
        self.assertIn("9876-5432-1098", html)
        # Check contract metadata
        self.assertIn(f"PLP-AGR-{self.contract.id:05d}", html)
        self.assertIn(str(self.contract.device_imei), html)
        self.assertIn(str(self.contract.monthly_emi_amount), html)
        # Check consent terms
        self.assertIn("Device Locking", html)
        self.assertIn("remotely locked", html)
        # Check customer signature is present
        self.assertIn("Borrower Digital Signature", html)
        self.assertIn("sig", html)
        # Verify retailer signature is removed and no table elements used
        self.assertNotIn("Authorized Signatory", html)
        self.assertNotIn("Retailer Signature", html)
        self.assertNotIn("<table", html)

    def test_auto_generated_agreement_json_format_and_serializer_url(self):
        self.client.force_authenticate(user=self.retailer)
        # Test JSON endpoint
        url = f'/api/emi-contracts/{self.contract.id}/agreement/?format=json'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertEqual(data['agreement_number'], f"PLP-AGR-{self.contract.id:05d}")
        self.assertIn('retailer', data)
        self.assertIn('customer', data)
        self.assertIn('financed_product', data)
        self.assertIn('collateral_device', data)
        self.assertIn('financial_terms', data)
        self.assertEqual(data['collateral_device']['device_imei'], self.contract.device_imei)

        # Test contract serializer exposes agreement_url
        contract_url = f'/api/emi-contracts/{self.contract.id}/'
        c_resp = self.client.get(contract_url)
        self.assertEqual(c_resp.status_code, status.HTTP_200_OK)
        self.assertIn('agreement_url', c_resp.data)
        self.assertTrue(c_resp.data['agreement_url'].endswith(f'/api/emi/contracts/{self.contract.id}/agreement/'))

    def test_auto_generated_agreement_pdf_download(self):
        self.client.force_authenticate(user=self.retailer)
        # Test direct PDF endpoint
        url = f'/api/emi-contracts/{self.contract.id}/agreement-pdf/'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertIn('inline; filename="EMI_Agreement_PLP_AGR_', resp['Content-Disposition'])
        self.assertTrue(resp.content.startswith(b'%PDF-'))
        self.assertGreater(len(resp.content), 1000)

        # Test query param ?download=pdf
        url_param = f'/api/emi-contracts/{self.contract.id}/agreement/?download=pdf'
        resp_param = self.client.get(url_param)
        self.assertEqual(resp_param.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_param['Content-Type'], 'application/pdf')
        self.assertTrue(resp_param.content.startswith(b'%PDF-'))

    def test_agreement_permission_enforcement(self):
        # Create unrelated customer
        other_customer = User.objects.create_user(
            username='intruder_customer',
            email='intruder@customer.com',
            role='CUSTOMER',
            password='Password123!'
        )
        self.client.force_authenticate(user=other_customer)
        url = f'/api/emi-contracts/{self.contract.id}/agreement/'
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

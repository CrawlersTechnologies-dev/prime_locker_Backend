from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
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

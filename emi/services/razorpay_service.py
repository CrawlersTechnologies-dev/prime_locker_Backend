"""
Razorpay Payment Gateway Service.
Handles Order creation, Webhook parsing, and Online Payment verification for EMI installments.
Supports live credentials via settings.RAZORPAY_CONFIG or operates gracefully in Simulation Mode.
"""
import hmac
import hashlib
import uuid
import logging
import requests
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)


class RazorpayService:
    @staticmethod
    def get_config():
        return getattr(settings, 'RAZORPAY_CONFIG', {
            'KEY_ID': '',
            'KEY_SECRET': '',
            'WEBHOOK_SECRET': ''
        })

    @classmethod
    def create_order(cls, installment):
        """
        Creates a Razorpay Order for an EMI installment.
        Returns the order payload required by the mobile app or web checkout modal.
        """
        config = cls.get_config()
        key_id = config.get('KEY_ID', '')
        key_secret = config.get('KEY_SECRET', '')

        amount_in_paise = int(installment.amount * 100)
        receipt_id = f"inst_{installment.id}"
        contract = installment.contract
        customer = contract.customer

        notes = {
            "installment_id": str(installment.id),
            "contract_id": str(contract.id),
            "device_imei": contract.device_imei,
            "customer_mobile": customer.mobile_number or ""
        }

        # If live credentials not configured yet, return a clean simulated order
        if not key_id or not key_secret:
            simulated_order_id = f"order_sim_{uuid.uuid4().hex[:14]}"
            logger.info(f"[SIMULATION] Razorpay Order created: {simulated_order_id} for Installment #{installment.id}")
            return {
                "order_id": simulated_order_id,
                "amount": amount_in_paise,
                "amount_display": str(installment.amount),
                "currency": "INR",
                "key_id": "rzp_test_simulation",
                "receipt": receipt_id,
                "customer": {
                    "name": f"{customer.first_name} {customer.last_name}".strip() or customer.username,
                    "contact": customer.mobile_number,
                    "email": customer.email
                },
                "notes": notes,
                "simulation": True,
                "message": "Order created in simulation mode. Add live RAZORPAY_KEY_ID in .env for live gateway."
            }

        # Live Razorpay Orders API call
        url = "https://api.razorpay.com/v1/orders"
        data = {
            "amount": amount_in_paise,
            "currency": "INR",
            "receipt": receipt_id,
            "notes": notes
        }

        try:
            resp = requests.post(url, json=data, auth=(key_id, key_secret), timeout=10)
            if resp.status_code in [200, 201]:
                order_data = resp.json()
                return {
                    "order_id": order_data.get('id'),
                    "amount": order_data.get('amount'),
                    "amount_display": str(installment.amount),
                    "currency": order_data.get('currency', 'INR'),
                    "key_id": key_id,
                    "receipt": receipt_id,
                    "customer": {
                        "name": f"{customer.first_name} {customer.last_name}".strip() or customer.username,
                        "contact": customer.mobile_number,
                        "email": customer.email
                    },
                    "notes": notes,
                    "simulation": False
                }
            else:
                logger.error(f"Razorpay Order Creation Failed: {resp.status_code} - {resp.text}")
                return {
                    "error": "Failed to create Razorpay order",
                    "details": resp.json() if resp.headers.get('content-type') == 'application/json' else resp.text
                }
        except requests.RequestException as e:
            logger.error(f"Network error calling Razorpay API: {e}")
            return {"error": "Razorpay connection error", "detail": str(e)}

    @classmethod
    def verify_signature(cls, order_id, payment_id, signature):
        """Verifies frontend checkout response signature"""
        config = cls.get_config()
        key_secret = config.get('KEY_SECRET', '')

        # In simulation mode, accept simulated orders
        if not key_secret or order_id.startswith('order_sim_') or signature == 'simulated_signature':
            return True

        message = f"{order_id}|{payment_id}".encode('utf-8')
        generated = hmac.new(key_secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(generated, signature)

    @classmethod
    def verify_webhook_signature(cls, raw_body_bytes, signature_header):
        """Verifies Razorpay Webhook signature"""
        config = cls.get_config()
        secret = config.get('WEBHOOK_SECRET', '')

        # Allow simulation if secret is not yet configured
        if not secret:
            return True

        if not signature_header:
            return False

        generated = hmac.new(secret.encode('utf-8'), raw_body_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(generated, signature_header)

    @classmethod
    def mark_installment_paid(cls, installment, amount, payment_id, notes="", user=None):
        """
        Executes atomic database updates upon successful online payment.
        """
        contract = installment.contract
        from emi.models import InstallmentStatus, EMIContractStatus, PaymentMode
        from accounts.models import AuditLog

        with transaction.atomic():
            installment.paid_amount = Decimal(str(amount))
            installment.status = InstallmentStatus.PAID
            installment.payment_mode = PaymentMode.ONLINE_GATEWAY
            installment.collection_date = timezone.now()
            installment.transaction_ref = payment_id
            installment.notes = notes or f"Online Payment via Gateway ({payment_id})"
            if user:
                installment.collected_by = user
            installment.save()

            # Check if all contract installments are paid
            unpaid_count = contract.installments.exclude(status=InstallmentStatus.PAID).count()
            overdue_count = contract.installments.filter(status=InstallmentStatus.OVERDUE).count()

            if unpaid_count == 0:
                contract.status = EMIContractStatus.COMPLETED
                contract.save(update_fields=['status'])
            elif overdue_count == 0 and contract.status == EMIContractStatus.LOCKED:
                contract.status = EMIContractStatus.ACTIVE
                contract.save(update_fields=['status'])

            # Bug B1 & Section B: When no overdue installments remain, restore device to ACTIVE and trigger unlock
            if overdue_count == 0:
                from devices.models import Device, DeviceLockStatus, DeviceCommand, CommandType, CommandStatus, LockProvider
                from devices.services.rocketpay_service import RocketPayService
                from accounts.views import send_system_notification
                from accounts.models import NotificationType

                device = Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
                if device and device.status == DeviceLockStatus.LOCKED:
                    device.status = DeviceLockStatus.ACTIVE
                    device.save(update_fields=['status'])

                    DeviceCommand.objects.create(
                        device=device,
                        command_type=CommandType.UNLOCK,
                        status=CommandStatus.PENDING,
                        issued_by=user if (user and getattr(user, 'is_authenticated', False)) else None,
                        parameters={"reason": "Online payment captured - auto unlock"}
                    )

                    if device.lock_provider == LockProvider.ROCKETPAY:
                        RocketPayService.unlock_device(
                            device=device,
                            reason="Online payment captured - auto unlock",
                            user=user
                        )

                    send_system_notification(
                        recipient=contract.retailer,
                        title="Device Auto-Unlocked",
                        message=f"Device {device.imei} was auto-unlocked after online payment receipt ({payment_id}).",
                        notification_type=NotificationType.DEVICE_UNLOCK,
                        reference_id=device.id,
                        sender=user
                    )
                    send_system_notification(
                        recipient=contract.customer,
                        title="Device Unlocked",
                        message=f"Your device ({device.imei}) has been unlocked. Thank you for your online payment!",
                        notification_type=NotificationType.DEVICE_UNLOCK,
                        reference_id=device.id,
                        sender=user
                    )

            AuditLog.objects.create(
                user=user or contract.customer,
                action="ONLINE_EMI_PAYMENT_CAPTURED",
                details={
                    "installment_id": installment.id,
                    "contract_id": contract.id,
                    "payment_id": payment_id,
                    "amount": str(amount),
                    "gateway": "RAZORPAY"
                }
            )

        return True

"""
Cashfree Payment Gateway Integration Service (PG Orders v2023-08-01).
Handles Order creation (returning payment_session_id), order status verification,
webhook signature parsing, and atomic EMI payment capture.
Supports live credentials or operates seamlessly in Simulation Mode.
"""
import os
import hmac
import hashlib
import base64
import uuid
import logging
import requests
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)


class CashfreeService:
    @staticmethod
    def get_config():
        from accounts.models import PlatformSettings
        ps = PlatformSettings.get_settings()
        default_cfg = getattr(settings, 'CASHFREE_CONFIG', {})

        app_id = getattr(ps, 'cashfree_app_id', None) or default_cfg.get('APP_ID', '')
        secret_key = getattr(ps, 'cashfree_secret_key', None) or default_cfg.get('SECRET_KEY', '')
        api_version = default_cfg.get('API_VERSION', '2023-08-01')
        env = getattr(ps, 'cashfree_mode', None) or default_cfg.get('ENV', 'TEST')
        
        base_url = 'https://api.cashfree.com/pg' if env == 'PROD' else 'https://sandbox.cashfree.com/pg'

        return {
            'APP_ID': app_id,
            'SECRET_KEY': secret_key,
            'VERIFY_APP_ID': os.getenv('CASHFREE_VERIFY_CLIENT_ID', ''),
            'VERIFY_SECRET_KEY': os.getenv('CASHFREE_VERIFY_CLIENT_SECRET', ''),
            'API_VERSION': api_version,
            'BASE_URL': base_url,
            'ENV': env
        }

    @classmethod
    def is_simulation_allowed(cls):
        """Simulation mode is strictly restricted to DEBUG=True or test environments."""
        return bool(getattr(settings, 'DEBUG', False) or getattr(settings, 'CASHFREE_SIMULATION', False))

    @classmethod
    def is_simulation(cls):
        config = cls.get_config()
        if config['APP_ID'] and config['SECRET_KEY']:
            return False
        return cls.is_simulation_allowed()

    @classmethod
    def create_order(cls, installment, return_url=None):
        """
        Creates a Cashfree Order for an EMI installment.
        Calls POST /pg/orders.
        Always charges installment.total_due (including dynamic late fees).
        Returns payload with payment_session_id for Cashfree Web/Mobile Checkout SDK.
        """
        config = cls.get_config()
        app_id = config['APP_ID']
        secret_key = config['SECRET_KEY']
        base_url = config['BASE_URL']
        api_version = config['API_VERSION']

        contract = installment.contract
        customer = contract.customer
        # Use total_due (installment.amount + late_fee)
        amount_decimal = installment.total_due
        amount = float(amount_decimal)
        order_amount_str = f"{amount_decimal:.2f}"
        order_id = f"cf_inst_{installment.id}_{uuid.uuid4().hex[:8]}"

        customer_name = f"{customer.first_name} {customer.last_name}".strip() or customer.username
        customer_phone = str(customer.mobile_number or "+919999999999")
        if not customer_phone.startswith('+'):
            customer_phone = f"+91{customer_phone[-10:]}"
        customer_email = customer.email or f"{customer.mobile_number}@customer.primelocker.com"

        # Simulation Mode fallback if credentials not configured
        if cls.is_simulation():
            sim_session = f"session_sim_{uuid.uuid4().hex}"
            logger.info(f"[SIMULATION] Cashfree order created: {order_id} (Session: {sim_session})")
            return {
                "gateway": "CASHFREE",
                "order_id": order_id,
                "cf_order_id": f"cf_{uuid.uuid4().hex[:8]}",
                "payment_session_id": sim_session,
                "order_status": "ACTIVE",
                "order_amount": order_amount_str,
                "order_currency": "INR",
                "environment": config['ENV'],
                "customer": {
                    "customer_id": f"cust_{customer.id}",
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "customer_phone": customer_phone
                },
                "simulation": True,
                "message": "Order created in Cashfree simulation mode. Add CASHFREE_APP_ID & CASHFREE_SECRET_KEY in .env for live gateway."
            }

        url = f"{base_url}/orders"
        headers = {
            "x-client-id": app_id,
            "x-client-secret": secret_key,
            "x-api-version": api_version,
            "Content-Type": "application/json"
        }
        payload = {
            "order_id": order_id,
            "order_amount": amount,
            "order_currency": "INR",
            "customer_details": {
                "customer_id": f"cust_{customer.id}",
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone[-10:]
            },
            "order_meta": {
                "return_url": return_url or "https://primelocker.pro/payment-status?order_id={order_id}"
            },
            "order_note": f"Installment #{installment.installment_number} for IMEI {contract.device_imei} (Total Due: ₹{amount})"
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=12)
            if resp.status_code in [200, 201]:
                data = resp.json()
                return {
                    "gateway": "CASHFREE",
                    "order_id": data.get('order_id', order_id),
                    "cf_order_id": str(data.get('cf_order_id', '')),
                    "payment_session_id": data.get('payment_session_id'),
                    "order_status": data.get('order_status', 'ACTIVE'),
                    "order_amount": f"{Decimal(str(data.get('order_amount', amount))):.2f}",
                    "order_currency": data.get('order_currency', 'INR'),
                    "environment": config['ENV'],
                    "customer": {
                        "customer_id": f"cust_{customer.id}",
                        "customer_name": customer_name,
                        "customer_email": customer_email,
                        "customer_phone": customer_phone
                    },
                    "simulation": False,
                    "details": data
                }
            else:
                logger.error(f"Cashfree order creation failed ({resp.status_code}): {resp.text}")
                return {
                    "error": "Failed to create Cashfree order",
                    "details": resp.json() if 'application/json' in resp.headers.get('content-type', '') else resp.text
                }
        except requests.RequestException as e:
            logger.error(f"Network error calling Cashfree API: {e}")
            return {"error": "Cashfree connection error", "detail": str(e)}

    @classmethod
    def get_order_status(cls, order_id):
        """
        Queries Cashfree to verify the current payment status of an order.
        Calls GET /pg/orders/{order_id}.
        Rejects fake/simulated orders when live or sandbox credentials are configured.
        """
        config = cls.get_config()
        has_credentials = bool(config['APP_ID'] and config['SECRET_KEY'])

        # 1. If credentials are configured, ALWAYS verify against Cashfree API; never accept cf_sim_
        if has_credentials:
            if str(order_id).startswith('cf_sim_') or str(order_id).startswith('order_sim_'):
                logger.warning(f"[SECURITY] Refused simulated order ID '{order_id}' because Cashfree credentials are configured.")
                return {
                    "error": "Simulated order IDs are not permitted when Cashfree credentials are configured.",
                    "order_status": "FAILED"
                }

            url = f"{config['BASE_URL']}/orders/{order_id}"
            headers = {
                "x-client-id": config['APP_ID'],
                "x-client-secret": config['SECRET_KEY'],
                "x-api-version": config['API_VERSION']
            }
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "order_id": data.get('order_id'),
                        "cf_order_id": data.get('cf_order_id'),
                        "order_status": data.get('order_status'),
                        "order_amount": data.get('order_amount'),
                        "details": data
                    }
                return {"error": f"HTTP {resp.status_code}", "order_status": "FAILED", "details": resp.text}
            except Exception as e:
                return {"error": str(e), "order_status": "FAILED"}

        # 2. When credentials are NOT configured:
        # Simulation is strictly disallowed unless CASHFREE_SIMULATION=True is explicitly set in settings/tests
        if not getattr(settings, 'CASHFREE_SIMULATION', False):
            logger.error("[SECURITY] Cashfree credentials are not configured and simulation is disabled.")
            return {
                "error": "Cashfree payment gateway credentials are not configured on server.",
                "order_status": "FAILED"
            }

        # 3. Explicit test simulation environment (CASHFREE_SIMULATION=True)
        return {
            "order_id": order_id,
            "order_status": "PAID",
            "payment_id": f"cf_pay_{uuid.uuid4().hex[:8]}",
            "simulation": True
        }

    @classmethod
    def verify_webhook_signature(cls, raw_body_bytes, timestamp, signature):
        """
        Verifies Cashfree HMAC-SHA256 signature from webhook header:
        Signature is Base64(HMAC-SHA256(timestamp + raw_body, secret_key)).
        Refuses unsigned webhooks when not in DEBUG mode.
        """
        config = cls.get_config()
        secret = config['SECRET_KEY']

        if not secret:
            if not cls.is_simulation_allowed():
                logger.error("[SECURITY] Cashfree webhook rejected: SECRET_KEY is not configured and DEBUG=False.")
                return False
            # Allow simulation if secret is not yet configured and in DEBUG mode
            return True

        if not signature or not timestamp:
            return False

        try:
            ts_payload = timestamp.encode('utf-8') + raw_body_bytes
            computed = base64.b64encode(hmac.new(secret.encode('utf-8'), ts_payload, hashlib.sha256).digest()).decode('utf-8')
            return hmac.compare_digest(computed, signature)
        except Exception as e:
            logger.error(f"Error validating Cashfree webhook signature: {e}")
            return False

    @classmethod
    def mark_installment_paid(cls, installment, amount, payment_id, notes="", user=None):
        """
        Executes atomic database updates upon successful Cashfree payment:
        - Marks installment PAID
        - Updates financial tallies
        - Restores device to ACTIVE and auto-unlocks via transaction.on_commit
        - Permanently unregisters RocketPay key if all EMIs completed (RBI compliance)
        - Dispatches WhatsApp receipt & in-app notification
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
            installment.notes = notes or f"Online Payment via Cashfree ({payment_id})"
            if user:
                installment.collected_by = user
            installment.save()

            # Check if all contract installments are paid
            unpaid_count = contract.installments.exclude(status=InstallmentStatus.PAID).count()
            overdue_count = contract.installments.filter(status=InstallmentStatus.OVERDUE).count()

            if unpaid_count == 0:
                contract.status = EMIContractStatus.COMPLETED
                contract.save(update_fields=['status'])

                # RP10: If all contracts secured by this lock_device are COMPLETED, unregister RocketPay key (RBI requirement)
                from devices.models import LockProvider
                from django.db.models import Q
                target_dev = contract.target_lock_device or Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
                if target_dev and target_dev.lock_provider in [LockProvider.ROCKETPAY, LockProvider.PARTNER_MDM]:
                    other_active = CustomerEMIContract.objects.filter(
                        Q(lock_device=target_dev) | Q(device_imei=target_dev.imei)
                    ).exclude(id=contract.id).exclude(status=EMIContractStatus.COMPLETED).exists()
                    if not other_active:
                        target_dev_id = target_dev.id
                        def _trigger_on_commit_unregister():
                            from devices.models import Device, LockProvider
                            from devices.services.rocketpay_service import RocketPayService
                            from devices.services.partner_mdm_service import PartnerMDMService
                            d = Device.objects.filter(id=target_dev_id).first()
                            if d and d.provider_device_id:
                                if d.lock_provider == LockProvider.ROCKETPAY:
                                    RocketPayService.unregister_key(d)
                                elif d.lock_provider == LockProvider.PARTNER_MDM:
                                    PartnerMDMService.remove_device(d)
                        transaction.on_commit(_trigger_on_commit_unregister)
            elif overdue_count == 0 and contract.status == EMIContractStatus.LOCKED:
                contract.status = EMIContractStatus.ACTIVE
                contract.save(update_fields=['status'])

            # Restore device to ACTIVE and trigger RocketPay unlock via transaction.on_commit
            if overdue_count == 0:
                from devices.models import Device, DeviceLockStatus, DeviceCommand, CommandType, CommandStatus, LockProvider
                from devices.services.rocketpay_service import RocketPayService
                from accounts.views import send_system_notification
                from accounts.models import NotificationType

                device = contract.target_lock_device or Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
                if device and device.status == DeviceLockStatus.LOCKED:
                    device.status = DeviceLockStatus.ACTIVE
                    device.save(update_fields=['status'])

                    DeviceCommand.objects.create(
                        device=device,
                        command_type=CommandType.UNLOCK,
                        status=CommandStatus.PENDING,
                        issued_by=user if (user and getattr(user, 'is_authenticated', False)) else None,
                        parameters={"reason": "Cashfree payment captured - auto unlock"}
                    )

                    dev_id = device.id
                    ret_id = contract.retailer_id
                    cust_id = contract.customer_id
                    u_id = user.id if (user and getattr(user, 'is_authenticated', False)) else None

                    def _trigger_on_commit_cashfree_unlock():
                        from devices.models import Device, LockProvider
                        from devices.services.rocketpay_service import RocketPayService
                        from accounts.views import send_system_notification
                        from accounts.models import NotificationType, User
                        dev = Device.objects.filter(id=dev_id).first()
                        if not dev:
                            return
                        u = User.objects.filter(id=u_id).first() if u_id else None
                        if dev.lock_provider == LockProvider.ROCKETPAY and dev.provider_status != "UNREGISTERED":
                            RocketPayService.unlock_device(
                                device=dev,
                                reason="Cashfree payment captured - auto unlock",
                                user=u
                            )
                        elif dev.lock_provider == LockProvider.PARTNER_MDM and dev.provider_status != "UNREGISTERED":
                            from devices.services.partner_mdm_service import PartnerMDMService
                            PartnerMDMService.unlock_device(
                                device=dev,
                                reason="Cashfree payment captured - auto unlock",
                                user=u
                            )
                        r = User.objects.filter(id=ret_id).first()
                        c = User.objects.filter(id=cust_id).first()
                        if r:
                            send_system_notification(
                                recipient=r,
                                title="Device Auto-Unlocked",
                                message=f"Device {dev.imei} was auto-unlocked after Cashfree payment receipt ({payment_id}).",
                                notification_type=NotificationType.DEVICE_UNLOCK,
                                reference_id=dev.id,
                                sender=u
                            )
                        if c:
                            send_system_notification(
                                recipient=c,
                                title="Device Unlocked",
                                message=f"Your device ({dev.imei}) has been unlocked. Thank you for your online payment!",
                                notification_type=NotificationType.DEVICE_UNLOCK,
                                reference_id=dev.id,
                                sender=u
                            )

                    transaction.on_commit(_trigger_on_commit_cashfree_unlock)

            AuditLog.objects.create(
                user=user or contract.customer,
                action="ONLINE_EMI_PAYMENT_CAPTURED",
                details={
                    "installment_id": installment.id,
                    "contract_id": contract.id,
                    "payment_id": payment_id,
                    "amount": str(amount),
                    "gateway": "CASHFREE"
                }
            )

        return True

    @staticmethod
    def normalize_mandate_status(raw_status):
        """Maps Cashfree subscription/mandate statuses into COMPLETED, PENDING, or FAILED."""
        norm = str(raw_status or '').strip().upper()
        if norm in ('COMPLETED', 'ACTIVE', 'AUTHORIZED', 'AUTHENTICATED', 'SUCCESS', 'VERIFIED'):
            return 'COMPLETED'
        if norm in ('FAILED', 'CANCELLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'BANK_APPROVAL_FAILED'):
            return 'FAILED'
        return 'PENDING'

    @classmethod
    def verify_bank_account(cls, account_number, ifsc_code, account_holder_name, mobile_number=None):
        """
        Verifies bank account details using Cashfree Verification API if enabled,
        or Cashfree Simulation Mode in DEBUG/test environments.
        Returns verification status: VERIFIED, PENDING (needs manual admin verify), or FAILED.
        """
        config = cls.get_config()
        if cls.is_simulation():
            return {
                "success": True,
                "verified": True,
                "status": "VERIFIED",
                "reference_id": f"bav_sim_{uuid.uuid4().hex[:10]}",
                "name_at_bank": account_holder_name,
                "simulation": True,
                "message": "Bank account verified in simulation mode."
            }

        verify_client_id = config.get('VERIFY_APP_ID') or config['APP_ID']
        verify_client_secret = config.get('VERIFY_SECRET_KEY') or config['SECRET_KEY']
        if not verify_client_id or not verify_client_secret:
            return {
                "success": True,
                "verified": False,
                "status": "PENDING",
                "reference_id": None,
                "simulation": False,
                "message": "Bank details saved and queued for admin verification."
            }

        verify_base = 'https://api.cashfree.com' if config['ENV'] == 'PROD' else 'https://sandbox.cashfree.com'
        url = f"{verify_base}/verification/bank-account/sync"
        headers = {
            "x-client-id": verify_client_id,
            "x-client-secret": verify_client_secret,
            "Content-Type": "application/json",
        }
        payload = {
            "bank_account": str(account_number).strip(),
            "ifsc": str(ifsc_code).strip().upper(),
            "name": str(account_holder_name).strip(),
        }
        if mobile_number:
            payload["phone"] = str(mobile_number).strip()[-10:]

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=12)
            data = resp.json() if 'application/json' in resp.headers.get('content-type', '') else {}
            if resp.status_code in (200, 201):
                acct_status = str(data.get('account_status', '')).upper()
                if acct_status == 'VALID':
                    return {
                        "success": True,
                        "verified": True,
                        "status": "VERIFIED",
                        "reference_id": str(data.get('reference_id') or data.get('utr') or ''),
                        "name_at_bank": data.get('name_at_bank') or account_holder_name,
                        "simulation": False,
                        "data": data,
                        "message": "Bank account verified successfully with Cashfree."
                    }
                return {
                    "success": False,
                    "verified": False,
                    "status": "FAILED",
                    "reference_id": str(data.get('reference_id') or ''),
                    "simulation": False,
                    "data": data,
                    "message": data.get('account_status_code') or data.get('message') or "Bank account verification failed."
                }
            # If Verification Suite is not enabled on PG-only credentials, keep PENDING for admin review
            logger.warning(f"Cashfree Bank Verification returned HTTP {resp.status_code}: {resp.text}")
            return {
                "success": True,
                "verified": False,
                "status": "PENDING",
                "reference_id": None,
                "simulation": False,
                "message": "Bank details submitted. Awaiting admin/gateway verification."
            }
        except requests.RequestException as exc:
            logger.warning(f"Cashfree Bank Verification network error: {exc}")
            return {
                "success": True,
                "verified": False,
                "status": "PENDING",
                "reference_id": None,
                "simulation": False,
                "message": "Bank details saved and queued for verification."
            }

    @classmethod
    def _ensure_subscription_plan(cls, base_url, headers, plan_id, plan_name, max_amount="50000.00"):
        """Ensures an ON_DEMAND Cashfree subscription plan exists before creating a mandate."""
        try:
            max_amt = float(Decimal(str(max_amount or "50000.00")))
        except Exception:
            max_amt = 50000.0
        if max_amt <= 0:
            max_amt = 50000.0

        plan_url = f"{base_url}/plans"
        plan_payload = {
            "plan_id": str(plan_id)[:50],
            "plan_name": str(plan_name or f"Plan {plan_id}")[:50],
            "plan_type": "ON_DEMAND",
            "plan_currency": "INR",
            "plan_max_amount": max_amt,
            "plan_note": "Prime Locker UPI e-Mandate plan",
        }
        try:
            resp = requests.post(plan_url, json=plan_payload, headers=headers, timeout=12)
            if resp.status_code not in (200, 201, 409):
                logger.info(f"Cashfree Plan check ({plan_id}): HTTP {resp.status_code} - {resp.text[:200]}")
        except requests.RequestException as exc:
            logger.warning(f"Cashfree Plan creation warning: {exc}")

    @classmethod
    def _submit_subscription_mandate(
        cls,
        mandate_id,
        plan_id,
        customer_details,
        auth_mode="UPI",
        return_url=None,
        max_amount="0.00",
        plan_name=None,
    ):
        """Shared helper to create a Cashfree Subscription / UPI e-Mandate in simulation or live mode."""
        config = cls.get_config()
        if cls.is_simulation():
            sim_auth_url = f"https://sandbox.cashfree.com/pg/orders/mandate/{mandate_id}?auth_mode={auth_mode}"
            sim_umrn = f"UMRN{uuid.uuid4().hex[:10].upper()}"
            logger.info(f"[SIMULATION] Cashfree UPI Mandate created: {mandate_id} (UMRN: {sim_umrn})")
            return {
                "success": True,
                "gateway": "CASHFREE",
                "mandate_id": mandate_id,
                "mandate_reference_id": sim_umrn,
                "mandate_status": "PENDING_AUTH",
                "mandate_auth_url": sim_auth_url,
                "subscription_session_id": f"sub_session_sim_{uuid.uuid4().hex[:16]}",
                "mandate_umrn": sim_umrn,
                "auth_mode": auth_mode,
                "max_amount": str(max_amount),
                "frequency": "MONTHLY",
                "simulation": True,
                "message": "UPI Auto-Pay mandate generated in Cashfree simulation mode. Open auth URL to approve."
            }

        url = f"{config['BASE_URL']}/subscriptions"
        headers = {
            "x-client-id": config['APP_ID'],
            "x-client-secret": config['SECRET_KEY'],
            "x-api-version": config['API_VERSION'],
            "Content-Type": "application/json"
        }

        cls._ensure_subscription_plan(
            config['BASE_URL'],
            headers,
            plan_id=plan_id,
            plan_name=plan_name or f"Prime Locker {plan_id}",
            max_amount=max_amount,
        )

        raw_phone = str(customer_details.get('customer_phone') or '9999999999')
        phone_10 = ''.join(ch for ch in raw_phone if ch.isdigit())[-10:] or '9999999999'
        cf_customer = {
            "customer_name": customer_details.get('customer_name') or 'Prime Locker User',
            "customer_email": customer_details.get('customer_email') or 'user@primelocker.pro',
            "customer_phone": phone_10,
        }
        method_key = 'enach' if str(auth_mode).upper() == 'ENACH' else ('card' if str(auth_mode).upper() == 'CARD' else 'upi')

        payload = {
            "subscription_id": mandate_id,
            "customer_details": cf_customer,
            "plan_details": {
                "plan_id": str(plan_id)[:50],
            },
            "authorization_details": {
                "authorization_amount": 1,
                "authorization_amount_refund": True,
                "payment_methods": [method_key],
            },
            "subscription_meta": {
                "return_url": return_url or "https://primelocker.pro/mandate-status?sub_id={subscription_id}",
            },
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            data = resp.json()
            if resp.status_code in [200, 201]:
                auth_link = data.get('auth_link') or data.get('sub_auth_url') or data.get('payment_link') or None
                sub_id = data.get('subscription_id', mandate_id)
                cf_sub_id = str(data.get('cf_subscription_id') or '')
                return {
                    "success": True,
                    "gateway": "CASHFREE",
                    "mandate_id": sub_id,
                    "mandate_reference_id": data.get('umrn') or cf_sub_id or sub_id,
                    "mandate_status": "PENDING_AUTH",
                    "mandate_auth_url": auth_link,
                    "subscription_session_id": data.get('subscription_session_id'),
                    "cf_subscription_id": cf_sub_id,
                    "auth_mode": auth_mode,
                    "max_amount": str(max_amount),
                    "simulation": False,
                    "data": data
                }
            logger.error(f"Cashfree Mandate API error: {resp.status_code} - {data}")
            return {
                "success": False,
                "error": data.get('message', 'Failed to create Cashfree mandate'),
                "status_code": resp.status_code,
                "details": data
            }
        except Exception as e:
            logger.error(f"Cashfree Mandate Connection exception: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def create_upi_mandate(cls, contract, auth_mode="UPI", return_url=None):
        """
        Point 10: Cashfree UPI Auto-Pay / e-Mandate registration for automated monthly EMI deduction.
        """
        customer = contract.customer
        customer_name = f"{customer.first_name} {customer.last_name}".strip() or customer.username
        customer_phone = str(customer.mobile_number or "9999999999")
        phone_10 = ''.join(ch for ch in customer_phone if ch.isdigit())[-10:] or "9999999999"
        customer_email = customer.email or f"{phone_10}@customer.primelocker.com"

        prefix = "sub_sim" if cls.is_simulation() else "sub"
        mandate_id = f"{prefix}_{contract.id}_{uuid.uuid4().hex[:8]}"
        max_amt = str(contract.monthly_emi_amount)
        return cls._submit_subscription_mandate(
            mandate_id=mandate_id,
            plan_id=f"plan_{contract.plan_template_id or contract.id}",
            plan_name=f"EMI Contract {contract.id}",
            customer_details={
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": phone_10,
            },
            auth_mode=auth_mode,
            return_url=return_url,
            max_amount=max_amt,
        )

    @classmethod
    def create_retailer_upi_mandate(cls, bank_account, auth_mode="UPI", return_url=None, max_amount="50000.00"):
        """Registers a Cashfree UPI e-Mandate for a Retailer bank account."""
        retailer = bank_account.retailer
        raw_phone = str(bank_account.mobile_number or retailer.mobile_number or "9999999999")
        phone_10 = ''.join(ch for ch in raw_phone if ch.isdigit())[-10:] or "9999999999"
        prefix = "ret_sub_sim" if cls.is_simulation() else "ret_sub"
        mandate_id = f"{prefix}_{retailer.id}_{uuid.uuid4().hex[:8]}"
        try:
            plan_cap = int(float(Decimal(str(max_amount or "50000.00"))))
        except Exception:
            plan_cap = 50000
        plan_cap = max(plan_cap, 100)
        customer_details = {
            "customer_name": bank_account.account_holder_name,
            "customer_email": retailer.email or f"{retailer.username}@retailer.primelocker.com",
            "customer_phone": phone_10,
        }

        return cls._submit_subscription_mandate(
            mandate_id=mandate_id,
            plan_id=f"ret_upi_od_{plan_cap}",
            plan_name=f"Retailer UPI Mandate {plan_cap}",
            customer_details=customer_details,
            auth_mode=auth_mode,
            return_url=return_url,
            max_amount=str(max_amount),
        )

    @classmethod
    def get_mandate_status(cls, mandate_id):
        """Fetches status of a Cashfree mandate / subscription."""
        # Only simulate when simulation mode is explicitly enabled or mandate_id is marked simulated
        if cls.is_simulation() or str(mandate_id).startswith(('sub_sim_', 'ret_sub_sim_')):
            return {
                "success": True,
                "mandate_id": mandate_id,
                "status": "ACTIVE",
                "umrn": f"UMRN{uuid.uuid4().hex[:10].upper()}",
                "simulation": True
            }

        config = cls.get_config()
        url = f"{config['BASE_URL']}/subscriptions/{mandate_id}"
        headers = {
            "x-client-id": config['APP_ID'],
            "x-client-secret": config['SECRET_KEY'],
            "x-api-version": config['API_VERSION']
        }
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                auth_ref = (data.get('authorization_details') or {}).get('authorization_reference')
                return {
                    "success": True,
                    "mandate_id": mandate_id,
                    "status": data.get('subscription_status', 'PENDING_AUTH'),
                    "umrn": data.get('umrn') or auth_ref or data.get('cf_subscription_id'),
                    "data": data
                }
            return {"success": False, "status_code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

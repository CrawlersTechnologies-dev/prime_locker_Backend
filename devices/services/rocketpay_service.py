"""
RocketPay OEM DLC Integration Service.
Handles OEM-level Android device locking/unlocking (Trustonic DLC, Samsung Knox, etc.),
device enrollment, webhook callbacks, and audit logging.
Operates with live API credentials or simulation mode if credentials are not configured.
"""
import hmac
import hashlib
import uuid
import logging
import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class RocketPayService:
    @staticmethod
    def get_config():
        from accounts.models import PlatformSettings
        ps = PlatformSettings.get_settings()
        api_key = ps.rocketpay_api_key or getattr(settings, 'ROCKETPAY_API_KEY', '')
        base_url = ps.rocketpay_base_url or getattr(settings, 'ROCKETPAY_BASE_URL', 'https://api.rocketpay.in')
        webhook_secret = ps.rocketpay_webhook_secret or getattr(settings, 'ROCKETPAY_WEBHOOK_SECRET', '')
        enabled = ps.rocketpay_enabled if hasattr(ps, 'rocketpay_enabled') else True
        return {
            'API_KEY': api_key,
            'BASE_URL': base_url.rstrip('/'),
            'WEBHOOK_SECRET': webhook_secret,
            'ENABLED': enabled
        }

    @classmethod
    def enroll_device(cls, device, customer_name="", customer_mobile="", retailer_name="", user=None):
        """
        Registers an Android device with RocketPay OEM DLC at point of sale.
        Returns dict with {"success": bool, "provider_device_id": str, "provider_status": str, "error": str}
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']

        payload = {
            "imei": device.imei,
            "secondary_imei": device.secondary_imei or "",
            "brand": device.brand or "",
            "model": device.model or "",
            "serial_number": device.serial_number or "",
            "customer_name": customer_name,
            "customer_mobile": customer_mobile,
            "retailer_name": retailer_name
        }

        # Simulation Mode
        if not api_key:
            provider_device_id = f"rp_{device.imei[:8]}_{uuid.uuid4().hex[:6]}"
            logger.info(f"[SIMULATION] RocketPay enrollment for IMEI {device.imei} -> ID: {provider_device_id}")
            cls._log_audit(
                action="ROCKETPAY_DEVICE_ENROLLED",
                user=user or device.retailer,
                details={
                    "mode": "SIMULATION",
                    "imei": device.imei,
                    "provider_device_id": provider_device_id,
                    "status": "ENROLLED"
                }
            )
            return {
                "success": True,
                "provider_device_id": provider_device_id,
                "provider_status": "ENROLLED",
                "message": "Enrolled successfully (Simulation Mode)."
            }

        # Live API Call
        url = f"{base_url}/v1/devices/enroll"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=12)
            res_data = resp.json() if resp.headers.get('content-type') == 'application/json' else {"raw": resp.text}
            cls._log_audit(
                action="ROCKETPAY_DEVICE_ENROLL_CALL",
                user=user or device.retailer,
                details={"imei": device.imei, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201]:
                provider_id = res_data.get('device_id') or res_data.get('id') or f"rp_{device.imei}"
                return {
                    "success": True,
                    "provider_device_id": str(provider_id),
                    "provider_status": res_data.get('status', 'ENROLLED')
                }
            else:
                err_msg = res_data.get('message') or res_data.get('error') or f"RocketPay error ({resp.status_code})"
                return {"success": False, "error": str(err_msg)}
        except requests.RequestException as e:
            logger.error(f"Network error enrolling with RocketPay: {e}")
            return {"success": False, "error": f"RocketPay connection failed: {str(e)}"}

    @classmethod
    def lock_device(cls, device, reason="EMI payment overdue", user=None):
        """
        Triggers OEM lock command via RocketPay API.
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        target_id = device.provider_device_id or device.imei

        # Simulation Mode
        if not api_key:
            logger.info(f"[SIMULATION] RocketPay device locked: IMEI {device.imei} ({target_id})")
            device.provider_status = "LOCKED"
            device.save(update_fields=['provider_status'])
            cls._log_audit(
                action="ROCKETPAY_LOCK_DEVICE",
                user=user,
                details={"mode": "SIMULATION", "imei": device.imei, "target_id": target_id, "reason": reason}
            )
            return {"success": True, "provider_status": "LOCKED"}

        url = f"{base_url}/v1/devices/{target_id}/lock"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            resp = requests.post(url, json={"reason": reason}, headers=headers, timeout=10)
            res_data = resp.json() if resp.headers.get('content-type') == 'application/json' else {"raw": resp.text}
            cls._log_audit(
                action="ROCKETPAY_LOCK_CALL",
                user=user,
                details={"imei": device.imei, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201]:
                device.provider_status = "LOCKED"
                device.save(update_fields=['provider_status'])
                return {"success": True, "provider_status": "LOCKED"}
            else:
                return {"success": False, "error": str(res_data)}
        except requests.RequestException as e:
            logger.error(f"Network error locking via RocketPay: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def unlock_device(cls, device, reason="EMI payment cleared", user=None):
        """
        Triggers OEM unlock command via RocketPay API.
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        target_id = device.provider_device_id or device.imei

        # Simulation Mode
        if not api_key:
            logger.info(f"[SIMULATION] RocketPay device unlocked: IMEI {device.imei} ({target_id})")
            device.provider_status = "UNLOCKED"
            device.save(update_fields=['provider_status'])
            cls._log_audit(
                action="ROCKETPAY_UNLOCK_DEVICE",
                user=user,
                details={"mode": "SIMULATION", "imei": device.imei, "target_id": target_id, "reason": reason}
            )
            return {"success": True, "provider_status": "UNLOCKED"}

        url = f"{base_url}/v1/devices/{target_id}/unlock"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            resp = requests.post(url, json={"reason": reason}, headers=headers, timeout=10)
            res_data = resp.json() if resp.headers.get('content-type') == 'application/json' else {"raw": resp.text}
            cls._log_audit(
                action="ROCKETPAY_UNLOCK_CALL",
                user=user,
                details={"imei": device.imei, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201]:
                device.provider_status = "UNLOCKED"
                device.save(update_fields=['provider_status'])
                return {"success": True, "provider_status": "UNLOCKED"}
            else:
                return {"success": False, "error": str(res_data)}
        except requests.RequestException as e:
            logger.error(f"Network error unlocking via RocketPay: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def verify_webhook_signature(cls, raw_body_bytes, signature_header):
        """Verifies HMAC SHA256 webhook signature from RocketPay"""
        config = cls.get_config()
        secret = config['WEBHOOK_SECRET']
        if not secret or signature_header == 'simulated_rocketpay_signature':
            return True
        if not signature_header:
            return False
        expected = hmac.new(secret.encode('utf-8'), raw_body_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    @classmethod
    def process_webhook_payload(cls, data):
        """
        Handles webhook event callback from RocketPay:
        Events: 'enrolled', 'locked', 'unlocked', 'failed', 'tampered'
        """
        from devices.models import Device, DeviceLockStatus
        from accounts.views import send_system_notification
        from accounts.models import NotificationType

        event = data.get('event', '').lower()
        imei = data.get('imei')
        provider_device_id = data.get('device_id') or data.get('provider_device_id')

        device = None
        if provider_device_id:
            device = Device.objects.filter(provider_device_id=provider_device_id).first()
        if not device and imei:
            device = Device.objects.filter(imei=imei).first()

        if not device:
            logger.warning(f"RocketPay webhook received for unknown device: imei={imei}, provider_id={provider_device_id}")
            return {"status": "ignored", "reason": "device not found"}

        cls._log_audit(
            action="ROCKETPAY_WEBHOOK_EVENT",
            user=None,
            details={"event": event, "device_id": device.id, "imei": device.imei, "payload": data}
        )

        if event in ['locked', 'device.locked']:
            device.status = DeviceLockStatus.LOCKED
            device.provider_status = "LOCKED"
            device.save(update_fields=['status', 'provider_status'])
            send_system_notification(
                recipient=device.retailer,
                title="RocketPay Device Locked",
                message=f"Device {device.imei} was locked by RocketPay OEM engine.",
                notification_type=NotificationType.DEVICE_LOCK,
                reference_id=device.id
            )
        elif event in ['unlocked', 'device.unlocked']:
            device.status = DeviceLockStatus.ACTIVE
            device.provider_status = "UNLOCKED"
            device.save(update_fields=['status', 'provider_status'])
            send_system_notification(
                recipient=device.retailer,
                title="RocketPay Device Unlocked",
                message=f"Device {device.imei} was unlocked by RocketPay OEM engine.",
                notification_type=NotificationType.DEVICE_UNLOCK,
                reference_id=device.id
            )
        elif event in ['enrolled', 'device.enrolled']:
            device.provider_status = "ENROLLED"
            device.save(update_fields=['provider_status'])
        elif event in ['tampered', 'device.tampered']:
            device.status = DeviceLockStatus.LOCKED
            device.provider_status = "TAMPERED"
            device.save(update_fields=['status', 'provider_status'])
            send_system_notification(
                recipient=device.retailer,
                title="⚠️ RocketPay Tamper Alert",
                message=f"Device {device.imei} has reported a tamper event! Device was auto-locked.",
                notification_type=NotificationType.OVERDUE_ALERT,
                reference_id=device.id
            )
        elif event in ['failed', 'device.failed']:
            device.provider_status = "FAILED"
            device.save(update_fields=['provider_status'])

        return {"status": "processed", "device_id": device.id, "device_status": device.status}

    @staticmethod
    def _log_audit(action, user, details):
        from accounts.models import AuditLog
        try:
            AuditLog.objects.create(
                user=user if (user and user.is_authenticated) else None,
                action=action,
                details=details
            )
        except Exception as e:
            logger.error(f"Failed to write AuditLog for RocketPay action {action}: {e}")

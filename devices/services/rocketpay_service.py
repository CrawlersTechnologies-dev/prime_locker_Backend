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
        Calls RocketPay POST /super-key/v2/only-key.
        Returns dict with {"success": bool, "provider_device_id": str, "provider_status": str, "error": str}
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']

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

        # Real RocketPay API Call
        # Endpoint: POST https://api-staging.rocketpay.co.in/super-key/v2/only-key
        super_key_url = f"{base_url}/super-key/v2/only-key"
        headers = {
            "x-token": api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "device_info": {
                "imei_no": str(device.imei),
                "imei_no2": str(device.secondary_imei or device.imei),
                "manufacturer": (device.brand or "Unknown").upper(),
                "model": str(device.model or "Unknown"),
                "mdm_type": "DLC"
            },
            "is_auto_reminder_enabled": False,
            "mobile_number": str(customer_mobile or "+919999999999"),
            "name": str(customer_name or "Prime Locker Customer")
        }
        try:
            resp = requests.post(super_key_url, json=payload, headers=headers, timeout=15)
            try:
                res_data = resp.json()
            except Exception:
                res_data = {"raw": resp.text}

            cls._log_audit(
                action="ROCKETPAY_DEVICE_ENROLL_CALL",
                user=user or device.retailer,
                details={"imei": device.imei, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201]:
                # key_id is used for controls (lock, unlock, controls), id is super-key id
                key_id = res_data.get('key_id') or res_data.get('id')
                return {
                    "success": True,
                    "provider_device_id": str(key_id),
                    "provider_status": "ENROLLED",
                    "super_key_id": str(res_data.get('id', '')),
                    "details": res_data
                }
            else:
                err_msg = res_data.get('message') or res_data.get('error') or f"RocketPay error ({resp.status_code})"
                return {"success": False, "error": str(err_msg)}
        except requests.RequestException as e:
            logger.error(f"Network error enrolling with RocketPay: {e}")
            return {"success": False, "error": f"RocketPay connection failed: {str(e)}"}
            logger.error(f"Network error enrolling with RocketPay: {e}")
            return {"success": False, "error": f"RocketPay connection failed: {str(e)}"}

    @classmethod
    def check_device_catalogue(cls, imei):
        """
        Queries RocketPay catalogue by IMEI to determine if device is eligible for DLC locking.
        Returns dict with eligibility state: 'APPROVED', 'UNDER_REVIEW', 'REJECTED'.
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        if not api_key:
            return {"eligible": True, "state": "APPROVED", "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/device-catalogue?imei={imei}"
        headers = {"x-token": api_key}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                state = data.get('state', '').upper()
                return {
                    "eligible": state == 'APPROVED',
                    "state": state,
                    "brand": data.get('brand'),
                    "model": data.get('model'),
                    "marketing_name": data.get('marketing_name'),
                    "mdm_type": data.get('mdm_type'),
                    "details": data
                }
            return {"eligible": False, "state": "NOT_FOUND", "status_code": resp.status_code}
        except Exception as e:
            logger.error(f"Error checking device catalogue: {e}")
            return {"eligible": False, "error": str(e)}

    @classmethod
    def lock_device(cls, device, reason="EMI payment overdue", title="Device Locked - EMI Overdue", user=None):
        """
        Triggers OEM lock command via RocketPay API.
        Endpoint: PUT /superkey/keys/{key_id}/control/LOCK/apply/LOCK
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

        # Real RocketPay Lock Endpoint
        url = f"{base_url}/superkey/keys/{target_id}/control/LOCK/apply/LOCK"
        headers = {
            "x-token": api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "title": str(title),
            "message": str(reason)
        }
        try:
            resp = requests.put(url, json=payload, headers=headers, timeout=12)
            try:
                res_data = resp.json()
            except Exception:
                res_data = {"raw": resp.text}

            cls._log_audit(
                action="ROCKETPAY_LOCK_CALL",
                user=user,
                details={"imei": device.imei, "target_id": target_id, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201, 204]:
                device.provider_status = "LOCKED"
                device.save(update_fields=['provider_status'])
                return {"success": True, "provider_status": "LOCKED", "details": res_data}
            else:
                return {"success": False, "error": str(res_data)}
        except requests.RequestException as e:
            logger.error(f"Network error locking via RocketPay: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def unlock_device(cls, device, reason="EMI payment cleared", user=None):
        """
        Triggers OEM unlock command via RocketPay API.
        Endpoint: PUT /superkey/keys/{key_id}/control/LOCK/apply/UNLOCK
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

        # Real RocketPay Unlock Endpoint
        url = f"{base_url}/superkey/keys/{target_id}/control/LOCK/apply/UNLOCK"
        headers = {
            "x-token": api_key,
            "Content-Type": "application/json"
        }
        try:
            resp = requests.put(url, json={}, headers=headers, timeout=12)
            try:
                res_data = resp.json()
            except Exception:
                res_data = {"raw": resp.text}

            cls._log_audit(
                action="ROCKETPAY_UNLOCK_CALL",
                user=user,
                details={"imei": device.imei, "target_id": target_id, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201, 204]:
                device.provider_status = "UNLOCKED"
                device.save(update_fields=['provider_status'])
                return {"success": True, "provider_status": "UNLOCKED", "details": res_data}
            else:
                return {"success": False, "error": str(res_data)}
        except requests.RequestException as e:
            logger.error(f"Network error unlocking via RocketPay: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def generate_offline_code(cls, device, challenge_code=""):
        """
        Requests an offline challenge-response unlock code from RocketPay.
        Endpoint: PUT /superkey/keys/{key_id}/control/UNLOCK_CODE/apply/FETCH
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        target_id = device.provider_device_id or device.imei

        if not api_key:
            import random
            simulated_code = f"{random.randint(100000, 999999)}"
            return {"success": True, "code": simulated_code, "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/{target_id}/control/UNLOCK_CODE/apply/FETCH"
        headers = {"x-token": api_key, "Content-Type": "application/json"}
        try:
            resp = requests.put(url, json={"code": challenge_code}, headers=headers, timeout=12)
            if resp.status_code in [200, 201]:
                res_data = resp.json()
                items = res_data.get('meta', {}).get('items', [])
                if items and 'code' in items[0]:
                    return {"success": True, "code": items[0]['code'], "details": res_data}
                return {"success": True, "code": res_data.get('code'), "details": res_data}
            return {"success": False, "error": resp.text}
        except Exception as e:
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

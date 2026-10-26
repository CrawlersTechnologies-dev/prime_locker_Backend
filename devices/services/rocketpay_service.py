"""
RocketPay OEM DLC Integration Service.
Handles OEM-level Android device locking/unlocking (Trustonic DLC, Samsung Knox, etc.),
device enrollment, webhook callbacks, and audit logging.
Operates with live API credentials or simulation mode if credentials are not configured.
"""
import hmac
import hashlib
import uuid
import os
import logging
import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class RocketPayService:
    @staticmethod
    def is_simulation_allowed():
        """Returns True only when ROCKETPAY_SIMULATION=true is explicitly configured in settings or environment."""
        sim_env = os.getenv('ROCKETPAY_SIMULATION', '').strip().lower()
        return sim_env in ['true', '1', 'yes'] or getattr(settings, 'ROCKETPAY_SIMULATION', False)

    @classmethod
    def get_config(cls):
        from accounts.models import PlatformSettings
        ps = PlatformSettings.get_settings()
        api_key = ps.rocketpay_api_key or getattr(settings, 'ROCKETPAY_API_KEY', '')
        base_url = ps.rocketpay_base_url or getattr(settings, 'ROCKETPAY_BASE_URL', 'https://api-staging.rocketpay.co.in')
        webhook_secret = ps.rocketpay_webhook_secret or getattr(settings, 'ROCKETPAY_WEBHOOK_SECRET', '')
        enabled = ps.rocketpay_enabled if hasattr(ps, 'rocketpay_enabled') else True
        return {
            'API_KEY': api_key,
            'BASE_URL': base_url.rstrip('/'),
            'WEBHOOK_SECRET': webhook_secret,
            'ENABLED': enabled
        }

    @classmethod
    def _parse_response_data(cls, resp):
        """Fix for content-type: parses JSON if content-type starts with application/json or contains it."""
        content_type = resp.headers.get('content-type', '').lower()
        if content_type.startswith('application/json') or 'application/json' in content_type:
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    @classmethod
    def enroll_device(cls, device, customer_name="", customer_mobile="", retailer_name="", user=None, mdm_type="DLC", frp_email="", is_running=False):
        """
        Registers an Android device with RocketPay OEM DLC or DPC at point of sale.
        Calls RocketPay POST /super-key/v2/only-key.
        Returns dict with {"success": bool, "provider_device_id": str, "provider_status": str, "qr_code_data": str, "error": str}
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        mdm_type = (mdm_type or "DLC").upper()

        # No Silent Simulation: if no API key is set, fail unless ROCKETPAY_SIMULATION=true
        if not api_key:
            if not cls.is_simulation_allowed():
                return {
                    "success": False,
                    "error": "RocketPay API key is not configured and simulation is disabled. Set ROCKETPAY_SIMULATION=true to test offline."
                }
            provider_device_id = f"rp_{device.imei[:8]}_{uuid.uuid4().hex[:6]}"
            logger.info(f"[SIMULATION] RocketPay enrollment for IMEI {device.imei} -> ID: {provider_device_id}")
            cls._log_audit(
                action="ROCKETPAY_DEVICE_ENROLLED",
                user=user or device.retailer,
                details={
                    "mode": "SIMULATION",
                    "imei": device.imei,
                    "provider_device_id": provider_device_id,
                    "status": "PENDING",
                    "mdm_type": mdm_type
                }
            )
            sim_qr = f"https://rocketpay.co.in/qr/sim_{provider_device_id}" if mdm_type == "DPC" else None
            return {
                "success": True,
                "provider_device_id": provider_device_id,
                "provider_status": "PENDING",
                "journey": "PENDING",
                "qr_code_data": sim_qr,
                "message": "Enrolled successfully (Simulation Mode)."
            }

        # Real RocketPay API Call
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
                "mdm_type": mdm_type
            },
            "is_auto_reminder_enabled": False,
            "mobile_number": str(customer_mobile or "+919999999999"),
            "name": str(customer_name or "Prime Locker Customer")
        }
        if is_running:
            payload["running"] = True
        if frp_email:
            payload["frp"] = {"email_id": str(frp_email)}

        try:
            resp = requests.post(super_key_url, json=payload, headers=headers, timeout=15)
            res_data = cls._parse_response_data(resp)

            cls._log_audit(
                action="ROCKETPAY_DEVICE_ENROLL_CALL",
                user=user or device.retailer,
                details={"imei": device.imei, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201]:
                key_id = res_data.get('key_id') or res_data.get('id')
                qr_code_data = None
                # RP5: If DPC, check gateway_meta or fetch key v5
                if mdm_type == "DPC":
                    qr_code_data = res_data.get('gateway_meta', {}).get('qr_code_data')
                    if not qr_code_data and key_id:
                        v5_info = cls.get_key_v5(key_id)
                        qr_code_data = v5_info.get('gateway_meta', {}).get('qr_code_data')

                # RP4: Enrolment starts in PENDING until journey becomes REGISTERED
                return {
                    "success": True,
                    "provider_device_id": str(key_id),
                    "provider_status": "PENDING",
                    "journey": "PENDING",
                    "super_key_id": str(res_data.get('id', '')),
                    "qr_code_data": qr_code_data,
                    "details": res_data
                }
            else:
                err_msg = res_data.get('message') or res_data.get('error') or f"RocketPay error ({resp.status_code})"
                device.provider_status = "FAILED"
                device.provider_error = str(err_msg)
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": False, "error": str(err_msg)}
        except requests.RequestException as e:
            logger.error(f"Network error enrolling with RocketPay: {e}")
            device.provider_status = "FAILED"
            device.provider_error = str(e)
            device.save(update_fields=['provider_status', 'provider_error'])
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
            if not cls.is_simulation_allowed():
                return {"eligible": False, "error": "RocketPay API key is not configured."}
            from devices.models import ApprovedDeviceTAC
            tac = (imei or '')[:8]
            tac_entry = ApprovedDeviceTAC.objects.filter(tac=tac, active=True).first()
            if tac_entry:
                return {
                    "eligible": True,
                    "state": "APPROVED",
                    "brand": tac_entry.brand,
                    "model": tac_entry.model_number,
                    "marketing_name": tac_entry.marketing_name,
                    "mdm_type": "DLC",
                    "mode": "SIMULATION"
                }
            return {"eligible": False, "state": "NOT_FOUND", "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/device-catalogue?imei={imei}"
        headers = {"x-token": api_key}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = cls._parse_response_data(resp)
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

        # Simulation Mode check
        if not api_key:
            if not cls.is_simulation_allowed():
                device.provider_status = "FAILED"
                device.provider_error = "RocketPay API key not configured."
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": False, "error": "RocketPay API key not configured and simulation is disabled."}
            logger.info(f"[SIMULATION] RocketPay device locked: IMEI {device.imei} ({target_id})")
            device.provider_status = "LOCKED"
            device.provider_error = None
            device.save(update_fields=['provider_status', 'provider_error'])
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
            res_data = cls._parse_response_data(resp)

            cls._log_audit(
                action="ROCKETPAY_LOCK_CALL",
                user=user,
                details={"imei": device.imei, "target_id": target_id, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201, 204]:
                device.provider_status = "LOCKED"
                device.provider_error = None
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": True, "provider_status": "LOCKED", "details": res_data}
            else:
                err_msg = res_data.get('message') or res_data.get('error') or str(res_data)
                device.provider_status = "FAILED"
                device.provider_error = str(err_msg)
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": False, "error": str(err_msg)}
        except requests.RequestException as e:
            logger.error(f"Network error locking via RocketPay: {e}")
            device.provider_status = "FAILED"
            device.provider_error = str(e)
            device.save(update_fields=['provider_status', 'provider_error'])
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

        # Simulation Mode check
        if not api_key:
            if not cls.is_simulation_allowed():
                device.provider_status = "FAILED"
                device.provider_error = "RocketPay API key not configured."
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": False, "error": "RocketPay API key not configured and simulation is disabled."}
            logger.info(f"[SIMULATION] RocketPay device unlocked: IMEI {device.imei} ({target_id})")
            device.provider_status = "UNLOCKED"
            device.provider_error = None
            device.save(update_fields=['provider_status', 'provider_error'])
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
            res_data = cls._parse_response_data(resp)

            cls._log_audit(
                action="ROCKETPAY_UNLOCK_CALL",
                user=user,
                details={"imei": device.imei, "target_id": target_id, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201, 204]:
                device.provider_status = "UNLOCKED"
                device.provider_error = None
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": True, "provider_status": "UNLOCKED", "details": res_data}
            else:
                err_msg = res_data.get('message') or res_data.get('error') or str(res_data)
                device.provider_status = "FAILED"
                device.provider_error = str(err_msg)
                device.save(update_fields=['provider_status', 'provider_error'])
                return {"success": False, "error": str(err_msg)}
        except requests.RequestException as e:
            logger.error(f"Network error unlocking via RocketPay: {e}")
            device.provider_status = "FAILED"
            device.provider_error = str(e)
            device.save(update_fields=['provider_status', 'provider_error'])
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
            if not cls.is_simulation_allowed():
                return {"success": False, "error": "RocketPay API key not configured and simulation is disabled."}
            import random
            simulated_code = f"{random.randint(100000, 999999)}"
            return {"success": True, "code": simulated_code, "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/{target_id}/control/UNLOCK_CODE/apply/FETCH"
        headers = {"x-token": api_key, "Content-Type": "application/json"}
        try:
            resp = requests.put(url, json={"code": challenge_code}, headers=headers, timeout=12)
            if resp.status_code in [200, 201]:
                res_data = cls._parse_response_data(resp)
                items = res_data.get('meta', {}).get('items', [])
                if items and 'code' in items[0]:
                    return {"success": True, "code": items[0]['code'], "details": res_data}
                return {"success": True, "code": res_data.get('code'), "details": res_data}
            return {"success": False, "error": resp.text}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @classmethod
    def verify_webhook_signature(cls, raw_body_bytes, signature_header):
        """
        Verifies HMAC SHA256 webhook signature from RocketPay.
        Strict verification: Rejects EVERY webhook when no secret is configured.
        """
        config = cls.get_config()
        secret = config['WEBHOOK_SECRET']
        if not secret:
            logger.error("RocketPay webhook rejected: ROCKETPAY_WEBHOOK_SECRET is not configured on server.")
            return False
        if not signature_header:
            logger.warning("RocketPay webhook rejected: Missing signature header.")
            return False

        sig = signature_header.strip()
        if sig.startswith('sha256='):
            sig = sig[7:]

        expected = hmac.new(secret.encode('utf-8'), raw_body_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    @classmethod
    def process_webhook_payload(cls, data):
        """
        Handles webhook event callback from RocketPay:
        Matches RocketPay real event action names:
        LOCKED, UNLOCKED, REGISTERED, DEREGISTERED, TAMPERED, FAILED
        """
        from devices.models import Device, DeviceLockStatus
        from accounts.views import send_system_notification
        from accounts.models import NotificationType

        event = str(data.get('event') or data.get('action') or data.get('state') or '').lower()
        imei = data.get('imei') or data.get('device_info', {}).get('imei_no')
        provider_device_id = data.get('device_id') or data.get('provider_device_id') or data.get('key_id') or data.get('id')

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

        if event in ['locked', 'device.locked', 'key.locked']:
            device.status = DeviceLockStatus.LOCKED
            device.provider_status = "LOCKED"
            device.provider_error = None
            device.save(update_fields=['status', 'provider_status', 'provider_error'])
            send_system_notification(
                recipient=device.retailer,
                title="RocketPay Device Locked",
                message=f"Device {device.imei} was locked by RocketPay OEM engine.",
                notification_type=NotificationType.DEVICE_LOCK,
                reference_id=device.id
            )
        elif event in ['unlocked', 'device.unlocked', 'key.unlocked']:
            device.status = DeviceLockStatus.ACTIVE
            device.provider_status = "UNLOCKED"
            device.provider_error = None
            device.save(update_fields=['status', 'provider_status', 'provider_error'])
            send_system_notification(
                recipient=device.retailer,
                title="RocketPay Device Unlocked",
                message=f"Device {device.imei} was unlocked by RocketPay OEM engine.",
                notification_type=NotificationType.DEVICE_UNLOCK,
                reference_id=device.id
            )
        elif event in ['enrolled', 'registered', 'device.enrolled', 'key.registered']:
            device.provider_status = "ENROLLED"
            device.provider_error = None
            device.save(update_fields=['provider_status', 'provider_error'])
        elif event in ['deregistered', 'unregistered', 'released', 'key.deregistered']:
            device.provider_status = "UNENROLLED"
            device.status = DeviceLockStatus.UNENROLLED
            device.save(update_fields=['status', 'provider_status'])
        elif event in ['tampered', 'device.tampered', 'key.tampered']:
            device.status = DeviceLockStatus.LOCKED
            device.provider_status = "TAMPERED"
            device.provider_error = "Device tamper reported by RocketPay OEM."
            device.save(update_fields=['status', 'provider_status', 'provider_error'])
            send_system_notification(
                recipient=device.retailer,
                title="⚠️ RocketPay Tamper Alert",
                message=f"Device {device.imei} has reported a tamper event! Device was auto-locked.",
                notification_type=NotificationType.OVERDUE_ALERT,
                reference_id=device.id
            )
        elif event in ['failed', 'device.failed']:
            device.provider_status = "FAILED"
            device.provider_error = data.get('message') or data.get('error') or "RocketPay command failed"
            device.save(update_fields=['provider_status', 'provider_error'])

        return {"status": "processed", "device_id": device.id, "device_status": device.status}

    @classmethod
    def get_key_v5(cls, key_id):
        """
        Retrieves live key and device state from RocketPay SuperKey v5 API.
        Endpoint: GET /superkey/keys/v5/{key_id}
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        if not api_key:
            return {"status": "REGISTERED", "is_locked": False, "journey": "REGISTERED", "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/v5/{key_id}"
        headers = {"x-token": api_key}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return cls._parse_response_data(resp)
            return {"error": f"HTTP {resp.status_code}", "status_code": resp.status_code}
        except Exception as e:
            logger.error(f"Error fetching RocketPay key v5: {e}")
            return {"error": str(e)}

    @classmethod
    def get_actions(cls, key_id):
        """
        Retrieves action history for this key.
        Endpoint: GET /superkey/keys/{key_id}/actions
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        if not api_key:
            return {"items": [{"status": "COMPLETED", "action": "LOCK"}]}

        url = f"{base_url}/superkey/keys/{key_id}/actions"
        headers = {"x-token": api_key}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return cls._parse_response_data(resp)
            return {"error": f"HTTP {resp.status_code}", "items": []}
        except Exception as e:
            logger.error(f"Error fetching RocketPay actions: {e}")
            return {"error": str(e), "items": []}

    @classmethod
    def sync_device_status(cls, device):
        """
        Polls RocketPay key details and actions (RP2, RP3, RP4):
        - Syncs is_locked, status, and journey from /superkey/keys/v5/{key_id}
        - Syncs latest action state (PENDING / COMPLETED / FAILED) from /superkey/keys/{key_id}/actions
        """
        from devices.models import DeviceLockStatus
        target_id = device.provider_device_id or device.imei
        if not target_id:
            return {"success": False, "error": "No provider device ID"}

        v5_data = cls.get_key_v5(target_id)
        if v5_data.get('error') and not v5_data.get('status'):
            return {"success": False, "error": v5_data.get('error')}

        journey = str(v5_data.get('journey', '')).upper()
        is_locked = v5_data.get('is_locked')

        # RP4: Enrolment stays PENDING until journey is REGISTERED
        if journey and journey != 'REGISTERED' and device.provider_status == 'PENDING':
            device.provider_status = 'PENDING'
        elif journey == 'REGISTERED' and device.provider_status == 'PENDING':
            device.provider_status = 'REGISTERED'

        # Check action statuses (RP3)
        actions_data = cls.get_actions(target_id)
        items = actions_data.get('items') or actions_data.get('data') or []
        if isinstance(items, list) and items:
            latest_action = items[0]
            action_status = str(latest_action.get('status', '')).upper()
            action_name = str(latest_action.get('action') or latest_action.get('type') or '').upper()

            if action_status == 'COMPLETED':
                if action_name in ['LOCK', 'LOCKED'] or is_locked is True:
                    device.status = DeviceLockStatus.LOCKED
                    device.provider_status = "LOCKED"
                    device.provider_error = None
                elif action_name in ['UNLOCK', 'UNLOCKED'] or is_locked is False:
                    device.status = DeviceLockStatus.ACTIVE
                    device.provider_status = "UNLOCKED"
                    device.provider_error = None
            elif action_status == 'FAILED':
                device.provider_status = "FAILED"
                device.provider_error = latest_action.get('error') or latest_action.get('message') or "Action execution failed"
            elif action_status == 'PENDING':
                device.provider_status = "PENDING"
        elif is_locked is not None:
            if is_locked:
                device.status = DeviceLockStatus.LOCKED
                device.provider_status = "LOCKED"
            else:
                if device.status == DeviceLockStatus.LOCKED:
                    device.status = DeviceLockStatus.ACTIVE
                device.provider_status = "UNLOCKED"

        device.save(update_fields=['status', 'provider_status', 'provider_error'])
        return {
            "success": True,
            "device_status": device.status,
            "provider_status": device.provider_status,
            "journey": journey,
            "is_locked": is_locked
        }

    @classmethod
    def apply_control(cls, device, control, action, payload=None, user=None):
        """
        Forwards device hardware or app control commands to RocketPay (RP6, RP7):
        Endpoint: PUT /superkey/keys/{key_id}/control/{control}/apply/{action}
        Supported apps: WHATSAPP, INSTAGRAM, FACEBOOK, YOUTUBE, SNAPCHAT, TELEGRAM, CHROME, etc.
        Supported hardware: CAMERA, HARD_RESET, USB_DEBUGGING, AUDIO_WARNING, GET_LOCATION, etc.
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        target_id = device.provider_device_id or device.imei
        control = str(control).upper()
        action = str(action).upper()

        if not api_key:
            if not cls.is_simulation_allowed():
                return {"success": False, "error": "RocketPay API key is not configured and simulation is disabled."}
            logger.info(f"[SIMULATION] Applied RocketPay control {control} -> {action} on IMEI {device.imei}")
            cls._log_audit(
                action="ROCKETPAY_APPLY_CONTROL",
                user=user,
                details={"mode": "SIMULATION", "imei": device.imei, "control": control, "action": action}
            )
            return {"success": True, "control": control, "action": action, "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/{target_id}/control/{control}/apply/{action}"
        headers = {
            "x-token": api_key,
            "Content-Type": "application/json"
        }
        body = payload or {}
        try:
            resp = requests.put(url, json=body, headers=headers, timeout=12)
            res_data = cls._parse_response_data(resp)
            cls._log_audit(
                action="ROCKETPAY_APPLY_CONTROL_CALL",
                user=user,
                details={"imei": device.imei, "control": control, "action": action, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201, 204]:
                return {"success": True, "control": control, "action": action, "details": res_data}
            err_msg = res_data.get('message') or res_data.get('error') or f"RocketPay control error ({resp.status_code})"
            return {"success": False, "error": str(err_msg)}
        except requests.RequestException as e:
            logger.error(f"Network error applying RocketPay control {control}: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def get_controls_status(cls, device):
        """
        Reads current hardware & app control states from RocketPay (RP8):
        Endpoint: GET /superkey/keys/{key_id}/controls/status
        Returns: { "on": [...], "off": [...], "locked": [...], "unlocked": [...] }
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        target_id = device.provider_device_id or device.imei

        if not api_key:
            return {
                "on": ["CAMERA", "USB_DEBUGGING"],
                "off": ["HARD_RESET"],
                "locked": [],
                "unlocked": ["WHATSAPP", "INSTAGRAM", "FACEBOOK"],
                "mode": "SIMULATION"
            }

        url = f"{base_url}/superkey/keys/{target_id}/controls/status"
        headers = {"x-token": api_key}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return cls._parse_response_data(resp)
            return {"error": f"HTTP {resp.status_code}", "status_code": resp.status_code}
        except Exception as e:
            logger.error(f"Error fetching RocketPay controls status: {e}")
            return {"error": str(e)}

    @classmethod
    def unregister_key(cls, device, user=None):
        """
        Permanently unregisters key from phone when EMI contract is completed (RP10).
        Endpoint: POST /superkey/keys/{key_id}/actions/unregister
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']
        target_id = device.provider_device_id or device.imei

        if not api_key:
            if not cls.is_simulation_allowed():
                return {"success": False, "error": "RocketPay API key not configured and simulation disabled."}
            logger.info(f"[SIMULATION] Unregistered RocketPay key for IMEI {device.imei}")
            device.provider_status = "UNREGISTERED"
            device.save(update_fields=['provider_status'])
            cls._log_audit(
                action="ROCKETPAY_UNREGISTER_KEY",
                user=user,
                details={"mode": "SIMULATION", "imei": device.imei, "target_id": target_id}
            )
            return {"success": True, "provider_status": "UNREGISTERED", "mode": "SIMULATION"}

        url = f"{base_url}/superkey/keys/{target_id}/actions/unregister"
        headers = {"x-token": api_key, "Content-Type": "application/json"}
        try:
            resp = requests.post(url, json={}, headers=headers, timeout=12)
            res_data = cls._parse_response_data(resp)
            cls._log_audit(
                action="ROCKETPAY_UNREGISTER_KEY_CALL",
                user=user,
                details={"imei": device.imei, "target_id": target_id, "status_code": resp.status_code, "response": res_data}
            )
            if resp.status_code in [200, 201, 204]:
                device.provider_status = "UNREGISTERED"
                device.save(update_fields=['provider_status'])
                return {"success": True, "provider_status": "UNREGISTERED", "details": res_data}
            err = res_data.get('message') or res_data.get('error') or f"Unregister error ({resp.status_code})"
            return {"success": False, "error": str(err)}
        except requests.RequestException as e:
            logger.error(f"Network error unregistering RocketPay key: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def get_wallet_balance(cls):
        """
        Retrieves remaining RocketPay key inventory in the company wallet (RP11):
        Endpoint: GET /common/wallets/v2?product_type=KEY
        Returns outstanding count (keys remaining).
        """
        config = cls.get_config()
        api_key = config['API_KEY']
        base_url = config['BASE_URL']

        if not api_key:
            return {"outstanding": 150, "low_balance_warning": False, "mode": "SIMULATION"}

        url = f"{base_url}/common/wallets/v2?product_type=KEY"
        headers = {"x-token": api_key}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = cls._parse_response_data(resp)
                items = data.get('items') or data.get('data') or []
                outstanding = 0
                if isinstance(items, list) and items:
                    outstanding = items[0].get('outstanding', 0)
                elif 'outstanding' in data:
                    outstanding = data.get('outstanding', 0)
                return {
                    "outstanding": int(outstanding),
                    "low_balance_warning": int(outstanding) < 20,
                    "details": data
                }
            return {"outstanding": 0, "low_balance_warning": True, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.error(f"Error fetching RocketPay wallet: {e}")
            return {"outstanding": 0, "low_balance_warning": True, "error": str(e)}

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

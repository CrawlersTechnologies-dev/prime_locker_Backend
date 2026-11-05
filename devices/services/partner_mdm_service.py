"""
Partner MDM (Android Enterprise 6-Tap QR) Integration Service.
Handles device enrollment via Android Enterprise QR provisioning,
remote locking/unlocking, policy controls (hide application, social apps, camera, factory reset),
audio reminders, GPS tracking, and permanent release upon EMI completion.

Operates with live API credentials or simulation mode if credentials are not configured or SIMULATION=True.
"""
import uuid
import os
import logging
import requests
from datetime import timedelta
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class PartnerMDMService:
    _cached_token = None
    _token_expiry = None

    @classmethod
    def get_config(cls):
        cfg = getattr(settings, 'PARTNER_MDM_CONFIG', {})
        base_url = cfg.get('BASE_URL') or os.getenv('PARTNER_MDM_BASE_URL', '')
        username = cfg.get('USERNAME') or os.getenv('PARTNER_MDM_USERNAME', '')
        password = cfg.get('PASSWORD') or os.getenv('PARTNER_MDM_PASSWORD', '')
        
        sim_override = getattr(settings, 'PARTNER_MDM_SIMULATION', None)
        if sim_override is not None:
            sim_val = sim_override
        else:
            sim_val = cfg.get('SIMULATION', True)

        if isinstance(sim_val, str):
            sim_val = sim_val.strip().lower() in ('true', '1', 'yes')
        return {
            'BASE_URL': base_url.rstrip('/'),
            'USERNAME': username,
            'PASSWORD': password,
            'SIMULATION': bool(sim_val)
        }

    @classmethod
    def is_simulation_allowed(cls):
        cfg = cls.get_config()
        if cfg['SIMULATION']:
            return True
        if not cfg['BASE_URL'] or not cfg['USERNAME'] or not cfg['PASSWORD']:
            return True
        return False

    @classmethod
    def _is_sim_mode(cls):
        cfg = cls.get_config()
        if not cfg['BASE_URL'] or not cfg['USERNAME'] or not cfg['PASSWORD']:
            return True
        return bool(cfg.get('SIMULATION', False))

    @classmethod
    def get_token(cls):
        """Authenticates with Partner API via POST /v1/auth/login and caches the token."""
        if cls._is_sim_mode():
            if not cls._cached_token or not str(cls._cached_token).startswith('mock_jwt_token_'):
                cls._cached_token = f"mock_jwt_token_{uuid.uuid4().hex[:12]}"
                cls._token_expiry = timezone.now() + timedelta(hours=1)
            return cls._cached_token

        # Return cached live token if still valid
        if cls._cached_token and cls._token_expiry and timezone.now() < cls._token_expiry and not str(cls._cached_token).startswith('mock_jwt_token_'):
            return cls._cached_token

        cfg = cls.get_config()

        url = f"{cfg['BASE_URL']}/v1/auth/login"
        payload = {
            "username": cfg['USERNAME'],
            "password": cfg['PASSWORD']
        }
        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get('data', {}).get('access_token')
                if token:
                    cls._cached_token = token
                    cls._token_expiry = timezone.now() + timedelta(hours=23)
                    return token
            logger.error(f"Partner MDM login failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Network error during Partner MDM login: {e}")
            if cls.is_simulation_allowed():
                cls._cached_token = f"mock_jwt_token_{uuid.uuid4().hex[:12]}"
                cls._token_expiry = timezone.now() + timedelta(hours=1)
                return cls._cached_token
        return None

    @classmethod
    def _call_api(cls, method, endpoint, json_data=None, params=None):
        """Helper to call authenticated Partner API endpoints."""
        cfg = cls.get_config()
        token = cls.get_token()
        if not token:
            return {"success": False, "error": "Unable to authenticate with Partner MDM server."}

        url = f"{cfg['BASE_URL']}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        try:
            resp = requests.request(method, url, json=json_data, params=params, headers=headers, timeout=15)
            try:
                res_json = resp.json()
            except Exception:
                res_json = {"raw": resp.text}

            if resp.status_code in [200, 201, 202, 204]:
                return {"success": True, "data": res_json.get('data', res_json), "status_code": resp.status_code}
            err_msg = (res_json.get('error', {}) if isinstance(res_json, dict) else {}).get('message') or res_json.get('message') or f"HTTP {resp.status_code}"
            return {"success": False, "error": str(err_msg), "status_code": resp.status_code, "details": res_json}
        except requests.RequestException as e:
            logger.error(f"Network error calling Partner MDM {endpoint}: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def _extract_qr_url(cls, data):
        """
        Recursively searches for a QR code URL anywhere inside nested dicts or lists.
        Matches common keys: qr_url, qr_code, qr_code_url, provisioning_qr, qr.
        """
        if not data:
            return ""

        target_keys = {"qr_url", "qr_code", "qr_code_url", "provisioning_qr", "qr"}

        if isinstance(data, dict):
            for key, val in data.items():
                if str(key).lower() in target_keys:
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                    elif isinstance(val, dict):
                        nested = cls._extract_qr_url(val)
                        if nested:
                            return nested
            for val in data.values():
                if isinstance(val, (dict, list)):
                    found = cls._extract_qr_url(val)
                    if found:
                        return found
        elif isinstance(data, list):
            for item in data:
                found = cls._extract_qr_url(item)
                if found:
                    return found
        return ""

    @classmethod
    def enroll_device(cls, device, customer_name="", customer_mobile="", customer_address="", loan_data=None, user=None):
        """
        Registers a customer & phone with Partner MDM (Android Enterprise).
        Returns Android Enterprise 6-tap QR code URL and device ID.
        Endpoint: POST /v1/customers followed by POST /v1/devices
        """
        loan_data = loan_data or {}

        # Simulation Mode
        if cls._is_sim_mode():
            sim_device_id = f"pm_{device.imei[:8]}_{uuid.uuid4().hex[:6]}"
            sim_qr = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://partner-mdm.net/provision/{sim_device_id}"
            logger.info(f"[SIMULATION] Partner MDM enrolled IMEI {device.imei} -> ID: {sim_device_id}")
            cls._log_audit(
                action="PARTNER_MDM_DEVICE_ENROLLED",
                user=user or device.retailer,
                details={
                    "mode": "SIMULATION",
                    "imei": device.imei,
                    "provider_device_id": sim_device_id,
                    "status": "PENDING"
                }
            )
            return {
                "success": True,
                "provider_device_id": sim_device_id,
                "provider_status": "PENDING",
                "qr_code_url": sim_qr,
                "mode": "SIMULATION"
            }

        # 1. Create or Find Customer on Partner Server
        clean_phone = ''.join(c for c in str(customer_mobile) if c.isdigit())[-10:] or "9999999999"
        cust_payload = {
            "customer_name": str(customer_name or "Prime Locker Customer")[:255],
            "phone_number": clean_phone,
            "address_text": str(customer_address or "Store Customer")[:1000],
            "auto_lock": True,
            "auto_reminder": True
        }
        cust_res = cls._call_api('POST', '/v1/customers', json_data=cust_payload)
        partner_customer_id = None
        if cust_res.get('success'):
            partner_customer_id = cust_res['data'].get('id')
        else:
            details = cust_res.get('details', {})
            err_details = details.get('error', {}).get('details', {}) if isinstance(details, dict) else {}
            partner_customer_id = err_details.get('customer_id')

        if not partner_customer_id:
            search_res = cls._call_api('GET', f'/v1/customers?search={clean_phone}')
            if search_res.get('success') and search_res.get('data'):
                partner_customer_id = search_res['data'][0].get('id')

        if not partner_customer_id:
            return {
                "success": False,
                "error": f"Failed to register customer with Partner MDM: {cust_res.get('error')}"
            }

        # 2. Register Phone on Partner Server (POST /v1/devices)
        device_payload = {
            "customer_id": partner_customer_id,
            "brand": str(device.brand or "Unknown").title(),
            "model": str(device.model or "Unknown"),
            "imei1": str(device.imei),
            "imei2": str(device.secondary_imei or ""),
            "device_type": "new",
            "finance": str(device.loan_provider or "In-house"),
            "device_price": float(loan_data.get('device_price') or 15000),
            "down_payment": float(loan_data.get('down_payment') or 0),
            "processing_fee": float(loan_data.get('processing_fee') or 0),
            "number_of_emi": int(loan_data.get('number_of_emi') or 6),
            "interest_rate": float(loan_data.get('interest_rate') or 0),
            "monthly_emi": float(loan_data.get('monthly_emi') or 2000),
            "first_emi_date": str(loan_data.get('first_emi_date') or timezone.localdate().isoformat())
        }
        dev_res = cls._call_api('POST', '/v1/devices', json_data=device_payload)
        if not dev_res.get('success'):
            return {
                "success": False,
                "error": f"Failed to register device with Partner MDM: {dev_res.get('error')}"
            }

        raw_data = dev_res.get('data') or {}
        dev_data = raw_data.get('device', raw_data) if isinstance(raw_data, dict) else {}
        partner_device_id = dev_data.get('id') if isinstance(dev_data, dict) else None
        if not partner_device_id and isinstance(raw_data, dict):
            partner_device_id = raw_data.get('id')

        qr_url = cls._extract_qr_url(raw_data)
        if not qr_url:
            logger.warning(
                f"[Partner MDM] Device {device.imei} enrolled successfully (provider ID {partner_device_id}), "
                f"but could not locate QR code URL in response payload: {raw_data}"
            )

        cls._log_audit(
            action="PARTNER_MDM_DEVICE_ENROLLED",
            user=user or device.retailer,
            details={
                "imei": device.imei,
                "provider_device_id": partner_device_id,
                "qr_url": qr_url
            }
        )

        return {
            "success": True,
            "provider_device_id": str(partner_device_id),
            "provider_status": "PENDING",
            "qr_code_url": qr_url,
            "details": dev_res['data']
        }

    @classmethod
    def lock_device(cls, device, reason="", user=None):
        """Locks the phone immediately via POST /v1/devices/{device_id}/lock."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Lock for {device.imei}")
            device.status = "LOCKED"
            device.provider_status = "LOCKED"
            device.save(update_fields=['status', 'provider_status'])
            cls._log_audit("PARTNER_MDM_LOCK", user, {"mode": "SIMULATION", "imei": device.imei})
            return {"success": True, "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/lock")
        if res.get('success'):
            device.status = "LOCKED"
            device.provider_status = "LOCKED"
            device.save(update_fields=['status', 'provider_status'])
            cls._log_audit("PARTNER_MDM_LOCK", user, {"imei": device.imei, "target_id": target_id, "reason": reason})
            return {"success": True, "data": res.get('data')}
        return {"success": False, "error": res.get('error')}

    @classmethod
    def unlock_device(cls, device, reason="", user=None):
        """Unlocks the phone immediately via POST /v1/devices/{device_id}/unlock."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Unlock for {device.imei}")
            device.status = "ACTIVE"
            device.provider_status = "UNLOCKED"
            device.save(update_fields=['status', 'provider_status'])
            cls._log_audit("PARTNER_MDM_UNLOCK", user, {"mode": "SIMULATION", "imei": device.imei})
            return {"success": True, "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/unlock")
        if res.get('success'):
            device.status = "ACTIVE"
            device.provider_status = "UNLOCKED"
            device.save(update_fields=['status', 'provider_status'])
            cls._log_audit("PARTNER_MDM_UNLOCK", user, {"imei": device.imei, "target_id": target_id, "reason": reason})
            return {"success": True, "data": res.get('data')}
        return {"success": False, "error": res.get('error')}

    @classmethod
    def update_policy(cls, device, policy_dict=None, user=None, **kwargs):
        """
        Updates restrictions (social apps, hide application, camera, factory reset, etc.)
        Endpoint: POST /v1/devices/{device_id}/policy
        """
        target_id = device.provider_device_id or device.imei
        policies = dict(policy_dict or {})
        policies.update(kwargs)

        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Policy update for {device.imei}: {policies}")
            cls._log_audit("PARTNER_MDM_UPDATE_POLICY", user, {"mode": "SIMULATION", "imei": device.imei, "policy": policies})
            return {"success": True, "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/policy", json_data=policies)
        if res.get('success'):
            cls._log_audit("PARTNER_MDM_UPDATE_POLICY", user, {"imei": device.imei, "policy": policies})
            return {"success": True, "data": res.get('data')}
        return {"success": False, "error": res.get('error')}

    @classmethod
    def audio_reminder(cls, device, user=None):
        """Plays loud EMI reminder sound on phone via POST /v1/devices/{device_id}/audio-reminder."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Audio Reminder for {device.imei}")
            return {"success": True, "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/audio-reminder")
        return {"success": res.get('success'), "data": res.get('data'), "error": res.get('error')}

    @classmethod
    def send_notification(cls, device, title, message, user=None):
        """Pushes custom reminder notification to device screen via POST /v1/devices/{device_id}/notification."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Notification for {device.imei}: {title} - {message}")
            return {"success": True, "mode": "SIMULATION"}

        payload = {"title": str(title)[:100], "message": str(message)[:500]}
        res = cls._call_api('POST', f"/v1/devices/{target_id}/notification", json_data=payload)
        return {"success": res.get('success'), "data": res.get('data'), "error": res.get('error')}

    @classmethod
    def reboot_device(cls, device, user=None):
        """Restarts the phone via POST /v1/devices/{device_id}/reboot."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Reboot for {device.imei}")
            return {"success": True, "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/reboot")
        return {"success": res.get('success'), "data": res.get('data'), "error": res.get('error')}

    @classmethod
    def refresh_location(cls, device, user=None):
        """Requests real-time GPS location refresh via POST /v1/devices/{device_id}/location/refresh."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Location Refresh for {device.imei}")
            return {"success": True, "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/location/refresh")
        return {"success": res.get('success'), "data": res.get('data'), "error": res.get('error')}

    @classmethod
    def get_location(cls, device):
        """Fetches last reported GPS coordinates via GET /v1/devices/{device_id}/location."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            return {
                "success": True,
                "available": True,
                "latitude": 23.0225,
                "longitude": 72.5714,
                "maps_url": "https://maps.google.com/?q=23.022500,72.571400",
                "mode": "SIMULATION"
            }

        res = cls._call_api('GET', f"/v1/devices/{target_id}/location")
        if res.get('success'):
            return {"success": True, "data": res.get('data')}
        return {"success": False, "error": res.get('error')}

    @classmethod
    def get_offline_code(cls, device):
        """Fetches 6-digit emergency offline unlock code via GET /v1/devices/{device_id}/offline-unlock-code."""
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            return {
                "success": True,
                "unlock_code": "235165",
                "offline_unlock_code": "235165",
                "valid_until": "10 minutes",
                "mode": "SIMULATION"
            }

        res = cls._call_api('GET', f"/v1/devices/{target_id}/offline-unlock-code")
        if res.get('success'):
            data = res.get('data', {})
            return {
                "success": True,
                "unlock_code": data.get('unlock_code'),
                "offline_unlock_code": data.get('unlock_code'),
                "valid_until": data.get('valid_until'),
                "message": data.get('message')
            }
        return {"success": False, "error": res.get('error')}

    @classmethod
    def remove_device(cls, device, user=None):
        """
        Permanently releases and removes MDM control once loan is closed.
        Endpoint: POST /v1/devices/{device_id}/remove with {"confirm": true}
        """
        target_id = device.provider_device_id or device.imei
        if cls._is_sim_mode():
            logger.info(f"[SIMULATION] Partner MDM Remove device for {device.imei}")
            device.status = "UNENROLLED"
            device.provider_status = "UNREGISTERED"
            device.save(update_fields=['status', 'provider_status'])
            cls._log_audit("PARTNER_MDM_REMOVE_DEVICE", user, {"mode": "SIMULATION", "imei": device.imei})
            return {"success": True, "provider_status": "UNREGISTERED", "mode": "SIMULATION"}

        res = cls._call_api('POST', f"/v1/devices/{target_id}/remove", json_data={"confirm": True})
        if res.get('success'):
            device.status = "UNENROLLED"
            device.provider_status = "UNREGISTERED"
            device.save(update_fields=['status', 'provider_status'])
            cls._log_audit("PARTNER_MDM_REMOVE_DEVICE", user, {"imei": device.imei, "target_id": target_id})
            return {"success": True, "provider_status": "UNREGISTERED", "data": res.get('data')}
        return {"success": False, "error": res.get('error')}

    @classmethod
    def _log_audit(cls, action, user, details):
        try:
            from accounts.models import AuditLog
            AuditLog.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                details=details
            )
        except Exception:
            pass

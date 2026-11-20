"""
Apple MDM / Supervised DEP Integration Service (Loksiz Apple MDM v1.4).
Official Base URL: https://prodapi.loksiz.com/loksiz/apple-mdm

Handles iPhone lifecycle management:
  - Service Health Check
  - iPhone Validation (IMEI & Serial Number)
  - Device Registration / Enrollment
  - Instant Remote Lock & Unlock
  - Application Lock / Unlock (Kiosk Mode)
  - Supervised Activation Lock (Enable / Disable / Status)
  - Wallpaper Management (Base64 Set & Remove)
  - Live GPS Location Refresh
  - Device Unenrollment / Deregistration
  - EMI Financing & Repayment Schedules
  - Real-time Webhook Receiver (apple_device_status_update)
"""
import logging
import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class AppleMDMService:
    @staticmethod
    def get_config():
        cfg = getattr(settings, 'APPLE_MDM_CONFIG', {})
        return {
            'BASE_URL': cfg.get('BASE_URL', 'https://prodapi.loksiz.com/loksiz/apple-mdm').rstrip('/'),
            'TOKEN': cfg.get('TOKEN', '') or cfg.get('API_KEY', ''),
        }

    @classmethod
    def _get_headers(cls):
        config = cls.get_config()
        token = config.get('TOKEN', '')
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    @classmethod
    def _request(cls, method, endpoint, payload=None, params=None):
        config = cls.get_config()
        base_url = config.get('BASE_URL')
        token = config.get('TOKEN')
        url = f"{base_url}/{endpoint.lstrip('/')}"

        # If no token configured, operate gracefully in simulation mode
        if not token:
            logger.info(f"[SIMULATION] Apple MDM {method} {url} with payload {payload}")
            return {
                "error": 0,
                "msg": f"[SIMULATION] Apple MDM {endpoint} simulated successfully.",
                "data": {"status": True, "simulated": True}
            }

        headers = cls._get_headers()
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, params=params, timeout=15)
            else:
                response = requests.post(url, headers=headers, json=payload or {}, params=params, timeout=15)

            try:
                data = response.json()
            except Exception:
                data = {
                    "error": 1 if response.status_code >= 400 else 0,
                    "msg": response.text,
                    "status_code": response.status_code
                }
            return data
        except requests.RequestException as exc:
            logger.error(f"Apple MDM API error ({method} {url}): {exc}")
            return {
                "error": 1,
                "msg": f"Failed to connect to Apple MDM Gateway: {str(exc)}",
                "data": None
            }

    # -------------------------------------------------------------------------
    # 5.1 Service Health Check
    # -------------------------------------------------------------------------
    @classmethod
    def health_check(cls):
        """GET /health - Verifies Apple MDM route and underlying controller are healthy."""
        return cls._request('GET', '/health')

    # -------------------------------------------------------------------------
    # 5.2 Device List & Dashboard Statistics
    # -------------------------------------------------------------------------
    @classmethod
    def get_devices(cls, page=1, limit=10, filter_status=None, search=None):
        """
        POST /get-devices
        filter_status: 0 (PENDING), 1 (ACTIVE), 2 (UNINSTALLED)
        search: list of dicts [{'key': 'customerName', 'value': 'John'}, ...] or single string
        """
        body = {
            "page": int(page),
            "limit": int(limit)
        }
        if filter_status is not None:
            body["filter"] = int(filter_status)
        if search:
            if isinstance(search, list):
                body["search"] = search
            elif isinstance(search, dict):
                body["search"] = [{"key": k, "value": v} for k, v in search.items()]
            else:
                body["search"] = [{"key": "customerName", "value": str(search)}]
        return cls._request('POST', '/get-devices', payload=body)

    # -------------------------------------------------------------------------
    # 5.3 Validate Device Eligibility (Mandatory pre-check before add-device)
    # -------------------------------------------------------------------------
    @classmethod
    def validate_device(cls, imei, serial_number, imei2=None):
        """
        POST /validate-device
        Checks iPhone eligibility, ensures no duplicates, and verifies wallet key balance.
        """
        body = {
            "imei": str(imei).strip(),
            "serialNumber": str(serial_number).strip()
        }
        if imei2:
            body["imei2"] = str(imei2).strip()
        return cls._request('POST', '/validate-device', payload=body)

    # -------------------------------------------------------------------------
    # 5.4 Add Device (Registers iPhone, consumes 1 IOS key, returns deviceRegNum)
    # -------------------------------------------------------------------------
    @classmethod
    def add_device(cls, imei, serial_number, customer_name="", customer_mobile="",
                   customer_email="", imei2=None):
        """
        POST /add-device
        Registers new Apple device into MDM platform and returns deviceRegNum.
        """
        body = {
            "imei": str(imei).strip(),
            "serialNumber": str(serial_number).strip(),
            "customerName": customer_name or "",
            "customerMobile": customer_mobile or "",
            "customerEmail": customer_email or ""
        }
        if imei2:
            body["imei2"] = str(imei2).strip()
        return cls._request('POST', '/add-device', payload=body)

    # -------------------------------------------------------------------------
    # 5.5 Update Device Info (Pending state only)
    # -------------------------------------------------------------------------
    @classmethod
    def update_device(cls, device_reg_num, customer_name=None, customer_mobile=None,
                      customer_email=None, imei=None, serial_number=None, imei2=None):
        """POST /update-device - Updates device & customer info while in PENDING state."""
        body = {"deviceRegNum": str(device_reg_num)}
        if customer_name: body["customerName"] = customer_name
        if customer_mobile: body["customerMobile"] = customer_mobile
        if customer_email: body["customerEmail"] = customer_email
        if imei: body["imei"] = str(imei).strip()
        if serial_number: body["serialNumber"] = str(serial_number).strip()
        if imei2: body["imei2"] = str(imei2).strip()
        return cls._request('POST', '/update-device', payload=body)

    # -------------------------------------------------------------------------
    # 5.6 Take Action (Lock / Unlock)
    # -------------------------------------------------------------------------
    @classmethod
    def take_action(cls, device_reg_num, action="LOCK", message=None,
                    phone_number=None, foot_note=None):
        """
        POST /take-action
        Executes immediate LOCK or UNLOCK on Apple MDM device.
        When LOCK, message, phoneNumber, and footNote display on Lost Mode screen.
        """
        act = action.upper()
        body = {
            "deviceRegNum": str(device_reg_num),
            "action": act
        }
        if act == "LOCK":
            if message:
                body["message"] = message
            if phone_number:
                body["phoneNumber"] = phone_number
            if foot_note:
                body["footNote"] = foot_note
        return cls._request('POST', '/take-action', payload=body)

    # -------------------------------------------------------------------------
    # 5.7 Uninstall / Unenroll Device Profile
    # -------------------------------------------------------------------------
    @classmethod
    def uninstall_device(cls, device_reg_num):
        """POST /uninstall-device - Removes MDM profile and unenrolls device."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/uninstall-device', payload=body)

    # -------------------------------------------------------------------------
    # 5.8 & 5.9 Application Lock & Unlock (Kiosk / Single App Mode)
    # -------------------------------------------------------------------------
    @classmethod
    def application_lock(cls, device_reg_num):
        """POST /application-lock-device - Enforces single app lock restriction."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/application-lock-device', payload=body)

    @classmethod
    def application_unlock(cls, device_reg_num):
        """POST /application-unlock-device - Removes application lock restriction."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/application-unlock-device', payload=body)

    # -------------------------------------------------------------------------
    # 5.10 - 5.15 Financial & EMI Management
    # -------------------------------------------------------------------------
    @classmethod
    def add_emi_details(cls, device_reg_num, loan_no, finance_type_bank, product_price,
                        down_payment, balance_amount, tenure, per_month_emi, due_date,
                        tenure_type="MONTHLY", interest_rate=0.0, other_charges=0.0,
                        total_emi_amount=None):
        """POST /add-emi-details - Associates loan parameters and generates schedule."""
        body = {
            "deviceRegNum": str(device_reg_num),
            "loanNo": str(loan_no),
            "financeTypeBank": str(finance_type_bank),
            "productPrice": float(product_price),
            "downPayment": float(down_payment),
            "otherCharges": float(other_charges),
            "balanceAmount": float(balance_amount),
            "tenure": int(tenure),
            "tenureType": str(tenure_type),
            "interestRate": float(interest_rate),
            "totalEmiAmount": float(total_emi_amount or (per_month_emi * tenure)),
            "perMonthEmi": float(per_month_emi),
            "dueDate": str(due_date)
        }
        return cls._request('POST', '/add-emi-details', payload=body)

    @classmethod
    def update_emi_details(cls, device_reg_num, **kwargs):
        """POST /update-emi-details - Updates loan and EMI configuration."""
        body = {"deviceRegNum": str(device_reg_num)}
        body.update(kwargs)
        return cls._request('POST', '/update-emi-details', payload=body)

    @classmethod
    def pay_emi(cls, device_reg_num, amount, payment_mode="UPI",
                transaction_reference="", remarks=""):
        """POST /pay-emi - Records an EMI installment payment."""
        body = {
            "deviceRegNum": str(device_reg_num),
            "amount": float(amount),
            "PaymentMode": str(payment_mode),
            "transactionReference": str(transaction_reference),
            "remarks": str(remarks)
        }
        return cls._request('POST', '/pay-emi', payload=body)

    @classmethod
    def get_emi_details(cls, device_reg_num):
        """POST /get-emi-details - Summary of loan status and counters."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/get-emi-details', payload=body)

    @classmethod
    def get_emi_payment_plan(cls, device_reg_num):
        """POST /get-emi-payment-plan - Itemized schedule breakdown."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/get-emi-payment-plan', payload=body)

    @classmethod
    def get_emi_payment_logs(cls, device_reg_num):
        """POST /get-emi-payment-logs - History of all processed payment transactions."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/get-emi-payment-logs', payload=body)

    # -------------------------------------------------------------------------
    # 5.16 & 5.17 Wallpaper Management
    # -------------------------------------------------------------------------
    @classmethod
    def set_wallpaper(cls, device_reg_num, wallpaper_image_base64):
        """POST /set-wallpaper - Sets wallpaper on Lock Screen and Home Screen."""
        body = {
            "deviceRegNum": str(device_reg_num),
            "wallpaperImage": wallpaper_image_base64
        }
        return cls._request('POST', '/set-wallpaper', payload=body)

    @classmethod
    def remove_wallpaper(cls, device_reg_num):
        """POST /remove-wallpaper - Resets device to default wallpaper."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/remove-wallpaper', payload=body)

    # -------------------------------------------------------------------------
    # 5.18 Device Live Location
    # -------------------------------------------------------------------------
    @classmethod
    def get_location(cls, device_reg_num):
        """POST /get-location - Fetches current GPS coordinates."""
        body = {"deviceRegNum": str(device_reg_num)}
        return cls._request('POST', '/get-location', payload=body)

    # -------------------------------------------------------------------------
    # 5.19 & 5.20 Activation Lock Control
    # -------------------------------------------------------------------------
    @classmethod
    def activation_lock(cls, device_reg_num, action=1):
        """
        POST /activation-lock
        action: 1 = Enable Activation Lock, 0 = Disable Activation Lock
        """
        body = {
            "deviceRegNum": str(device_reg_num),
            "action": int(action)
        }
        return cls._request('POST', '/activation-lock', payload=body)

    @classmethod
    def activation_lock_status(cls, device_reg_num):
        """POST /activation-lock-status?deviceRegNum=... - Queries Activation Lock state."""
        return cls._request('POST', '/activation-lock-status', params={"deviceRegNum": str(device_reg_num)})

    # -------------------------------------------------------------------------
    # DeviceCommand Dispatcher (Called by DeviceViewSet._create_command)
    # -------------------------------------------------------------------------
    @classmethod
    def send_command(cls, device, command_type, payload=None):
        """
        Sends an MDM command to the Apple Supervised device.
        Maps internal Prime Locker CommandType to Loksiz Apple MDM v1.4 endpoints.
        """
        device_reg = device.provider_device_id or device.serial_number or device.imei
        if not device_reg:
            logger.warning(f"Apple MDM send_command called with no identifier for device {device.id}")
            return {"error": 1, "msg": "Missing device identifier (provider_device_id/serial/imei)."}

        payload = payload or {}

        if command_type == 'LOCK':
            msg = payload.get('message') or device.lock_message or "Payment overdue. Please contact admin."
            phone = getattr(device.retailer, 'mobile_number', '') if device.retailer else ''
            return cls.take_action(
                device_reg_num=device_reg,
                action="LOCK",
                message=msg,
                phone_number=phone,
                foot_note="Prime Locker Protection"
            )
        elif command_type == 'UNLOCK':
            return cls.take_action(device_reg_num=device_reg, action="UNLOCK")
        elif command_type == 'SET_WALLPAPER':
            img_b64 = payload.get('wallpaperImage') or payload.get('imageBase64') or ''
            return cls.set_wallpaper(device_reg_num=device_reg, wallpaper_image_base64=img_b64)
        elif command_type == 'SYNC_LOCATION':
            return cls.get_location(device_reg_num=device_reg)
        elif command_type == 'UPDATE_POLICY':
            # Check if app lock was requested
            if payload.get('application_lock') is True:
                return cls.application_lock(device_reg_num=device_reg)
            elif payload.get('application_lock') is False:
                return cls.application_unlock(device_reg_num=device_reg)
            return {"error": 0, "msg": "Policy updated"}
        elif command_type == 'WIPE':
            return cls.uninstall_device(device_reg_num=device_reg)

        return {"error": 0, "msg": f"Apple MDM command '{command_type}' dispatched."}

    # -------------------------------------------------------------------------
    # 5.21 Webhook Payload Processor
    # -------------------------------------------------------------------------
    @classmethod
    def process_webhook_payload(cls, payload, headers=None):
        """
        Processes real-time webhook callback from Loksiz Apple MDM:
          Event: 'apple_device_status_update'
          Slugs:
            - 'lock_status': status 1 = LOCKED, 0 = ACTIVE (UNLOCKED)
            - 'activation_status': status 1 = ACTIVE (Enrolled), 0 = UNENROLLED
            - 'app_lock_status': status 1 = App Locked, 0 = Unlocked
            - 'wallpaper_status': status 1 = Applied, 0 = Removed
            - 'device_location': location { lat, long, date }
        """
        event = payload.get('event')
        if event != 'apple_device_status_update':
            logger.info(f"Ignoring non-apple MDM webhook event: {event}")
            return {"status": "ignored", "reason": f"unsupported_event_{event}"}

        device_reg = payload.get('deviceRegNum')
        imei = payload.get('imei')
        serial = payload.get('serialNumber')
        slug = payload.get('slug')
        status_val = payload.get('status')
        location = payload.get('location')
        callback_id = payload.get('callback_id')

        # Late imports to avoid circular dependencies
        from devices.models import Device, DeviceLockStatus, DeviceLocationLog
        from accounts.models import AuditLog

        device = None
        if device_reg:
            device = Device.objects.filter(provider_device_id=str(device_reg)).first()
        if not device and imei:
            device = Device.objects.filter(imei=str(imei)).first()
        if not device and serial:
            device = Device.objects.filter(serial_number=str(serial)).first()

        if not device:
            logger.warning(
                f"Apple MDM webhook device not found: reg={device_reg}, imei={imei}, serial={serial}"
            )
            return {"status": "success", "note": "device_not_registered_locally"}

        # Store provider_device_id if not already set
        if device_reg and not device.provider_device_id:
            device.provider_device_id = str(device_reg)

        update_fields = ['provider_device_id', 'last_online_at']
        device.last_online_at = timezone.now()

        if slug == 'lock_status':
            if status_val == 1:
                device.status = DeviceLockStatus.LOCKED
            elif status_val == 0:
                device.status = DeviceLockStatus.ACTIVE
            update_fields.append('status')

        elif slug == 'activation_status':
            if status_val == 1:
                device.status = DeviceLockStatus.ACTIVE
                device.is_activated = True
                if not device.activated_at:
                    device.activated_at = timezone.now()
                    update_fields.append('activated_at')
                update_fields.extend(['status', 'is_activated'])
            elif status_val == 0:
                device.status = DeviceLockStatus.UNENROLLED
                device.is_activated = False
                update_fields.extend(['status', 'is_activated'])

        elif slug == 'wallpaper_status':
            if status_val == 1:
                device.wallpaper_locked = True
            elif status_val == 0:
                device.wallpaper_locked = False
            update_fields.append('wallpaper_locked')

        elif slug == 'device_location':
            if location and isinstance(location, dict):
                lat = location.get('lat')
                lon = location.get('long')
                if lat is not None and lon is not None:
                    try:
                        device.current_latitude = lat
                        device.current_longitude = lon
                        device.last_location_at = timezone.now()
                        update_fields.extend(['current_latitude', 'current_longitude', 'last_location_at'])

                        DeviceLocationLog.objects.create(
                            device=device,
                            latitude=lat,
                            longitude=lon
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to record Apple MDM location: {exc}")

        device.save(update_fields=list(set(update_fields)))

        # Log system audit entry
        try:
            AuditLog.objects.create(
                user=device.customer or device.retailer,
                action="APPLE_MDM_STATUS_UPDATE",
                details={
                    "device_id": device.id,
                    "imei": device.imei,
                    "slug": slug,
                    "status": status_val,
                    "callback_id": callback_id
                }
            )
        except Exception:
            pass

        return {"status": "success"}

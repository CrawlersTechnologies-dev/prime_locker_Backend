from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db import transaction, models
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from .models import (
    Device,
    DeviceCommand,
    DeviceLocationLog,
    AppVersion,
    DeviceCategory,
    OSType,
    DeviceScannerState,
    DeviceLockStatus,
    LockProvider,
    CommandType,
    CommandStatus,
    AppType,
    InstallationVideo,
    VideoCategory
)
from .serializers import (
    DeviceSerializer,
    DeviceCommandSerializer,
    DeviceLocationLogSerializer,
    AppVersionSerializer,
    DeviceHeartbeatSerializer,
    InstallationVideoSerializer
)
from .imei_lookup import lookup_imei
from accounts.models import User, AuditLog
from accounts.views import get_client_ip, get_all_downstream_users
from decimal import Decimal
from emi.models import CustomerEMIContract, EMIContractStatus, EMIPlanTemplate, EMIInstallment, InstallmentStatus, add_months
from .services.rocketpay_service import RocketPayService

ROCKETPAY_APP_MAP = {
    'com.whatsapp': 'WHATSAPP',
    'com.whatsapp.w4b': 'WHATSAPP',
    'com.instagram.android': 'INSTAGRAM',
    'com.facebook.katana': 'FACEBOOK',
    'com.facebook.lite': 'FACEBOOK',
    'com.google.android.youtube': 'YOUTUBE',
    'com.snapchat.android': 'SNAPCHAT',
    'org.telegram.messenger': 'TELEGRAM',
    'com.android.chrome': 'CHROME',
    'com.truecaller': 'TRUECALLER',
    'in.startv.hotstar': 'HOTSTAR',
    'com.amazon.avod.thirdpartyclient': 'AMAZON_PRIME',
    'com.jio.media.ondemand': 'JIO_CINEMA',
    'com.phonepe.app': 'PHONEPE',
    'com.google.android.apps.nbu.paisa.user': 'GOOGLE_PAY',
    'net.one97.paytm': 'PAYTM',
    'in.org.npci.upiapp': 'BHIM',
    'com.google.android.apps.maps': 'GOOGLE_MAPS',
}

def is_rocketpay_running_phone(device):
    """Running phones (collateral) on RocketPay only support lock, unlock code, audio warning, location, and unregister."""
    return device.lock_provider == LockProvider.ROCKETPAY and device.device_state == DeviceScannerState.RUNNING


def get_master_offline_code(imei: str) -> str:
    """Deterministic 6-digit emergency master unlock fallback code based on IMEI and secret key"""
    if not imei:
        return ""
    import hmac, hashlib
    from django.conf import settings
    secret = getattr(settings, 'SECRET_KEY', 'prime-locker-secret-key').encode('utf-8')
    h = hmac.new(secret, str(imei).strip().encode('utf-8'), hashlib.sha256).hexdigest()
    digits = ''.join(c for c in h if c.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return (digits + "135790")[:6]


class IsSuperAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.role == 'SUPER_ADMIN'


class DeviceViewSet(viewsets.ModelViewSet):
    """
    Module 1: Mobile & TV Device Management and Remote Control Suite.
    Accessible across all tiers based on organizational hierarchy.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DeviceSerializer

    def get_queryset(self):
        user = self.request.user
        from accounts.models import is_company_user
        if is_company_user(user):
            qs = Device.objects.all()
        elif user.role == 'RETAILER':
            qs = Device.objects.filter(retailer=user)
        elif user.role == 'CUSTOMER':
            qs = Device.objects.filter(customer=user)
        else:
            downline_users = user.get_all_subordinates()
            retailer_ids = [u.id for u in downline_users if u.role == 'RETAILER']
            qs = Device.objects.filter(retailer_id__in=retailer_ids)

        # Optimize N+1 queries by pre-fetching related ForeignKey records touched by DeviceSerializer
        qs = qs.select_related('customer', 'retailer', 'activation_key', 'emi_contract')

        # Query Filters
        category = self.request.query_params.get('device_category')
        if category:
            qs = qs.filter(device_category=category.upper())

        os_filter = self.request.query_params.get('os_type')
        if os_filter:
            qs = qs.filter(os_type=os_filter.upper())

        state_filter = self.request.query_params.get('device_state')
        if state_filter:
            qs = qs.filter(device_state=state_filter.upper())

        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(imei__icontains=search) |
                Q(brand__icontains=search) |
                Q(model__icontains=search) |
                Q(device_name__icontains=search) |
                Q(customer__first_name__icontains=search) |
                Q(customer__last_name__icontains=search) |
                Q(customer__mobile_number__icontains=search)
            )

        online_filter = self.request.query_params.get('is_online')
        if online_filter is not None:
            cutoff = timezone.now() - timedelta(minutes=15)
            if online_filter.lower() in ['true', '1']:
                qs = qs.filter(last_online_at__gte=cutoff)
            else:
                qs = qs.filter(Q(last_online_at__isnull=True) | Q(last_online_at__lt=cutoff))

        return qs

    def perform_create(self, serializer):
        retailer = self.request.user if self.request.user.role == 'RETAILER' else serializer.validated_data.get('retailer', self.request.user)
        instance = serializer.save(retailer=retailer)
        AuditLog.objects.create(
            user=self.request.user,
            action="REGISTERED_DEVICE",
            ip_address=get_client_ip(self.request),
            details={"imei": instance.imei, "category": instance.device_category, "state": instance.device_state}
        )

    # -----------------------------
    # IMEI Auto-Lookup Utility
    # -----------------------------
    @action(detail=False, methods=['get'], url_path='imei-lookup')
    def imei_lookup(self, request):
        """
        Auto-detects Brand, Model, Device Category, and OS Type from a 15-digit IMEI.
        """
        imei = request.query_params.get('imei')
        if not imei:
            return Response({"detail": "Query parameter 'imei' is required."}, status=status.HTTP_400_BAD_REQUEST)

        result = lookup_imei(imei)
        return Response(result)

    # -----------------------------
    # Remote MDM Commands
    # -----------------------------
    def _create_command(self, device, command_type, params=None):
        cmd = DeviceCommand.objects.create(
            device=device,
            command_type=command_type,
            parameters=params or {},
            issued_by=self.request.user
        )
        if device.os_type == OSType.IOS:
            from .services.apple_mdm import AppleMDMService
            AppleMDMService.send_command(device, command_type, params)
        if getattr(device, 'fcm_token', None):
            try:
                from .services.fcm_service import FCMService
                FCMService.send_command(device, command_type, params)
            except Exception:
                pass
        return cmd

    def _block_customer(self, request):
        if request.user.role == 'CUSTOMER':
            return Response({"detail": "Customers do not have permission to control devices."}, status=status.HTTP_403_FORBIDDEN)
        if request.user.role == 'STAFF_VIEWER':
            return Response({"detail": "Viewer staff accounts have read-only access."}, status=status.HTTP_403_FORBIDDEN)
        return None

    @action(detail=True, methods=['post'])
    def lock(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device.status = DeviceLockStatus.LOCKED
        device.save(update_fields=['status'])
        self._create_command(device, CommandType.LOCK)

        # Sync linked EMI contract if exists
        if device.emi_contract:
            device.emi_contract.status = EMIContractStatus.LOCKED
            device.emi_contract.save(update_fields=['status'])

        reason = request.data.get('reason', '').strip()

        # Section B: Trigger RocketPay OEM DLC Lock if device is controlled by RocketPay
        if device.lock_provider == LockProvider.ROCKETPAY:
            from .services.rocketpay_service import RocketPayService
            RocketPayService.lock_device(device=device, reason=reason or "Remote lock from dashboard", user=request.user)
        elif device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.lock_device(device=device, reason=reason or "Remote lock from dashboard", user=request.user)
        elif device.lock_provider == LockProvider.APPLE_MDM or device.os_type == OSType.IOS:
            from .services.apple_mdm import AppleMDMService
            phone = getattr(device.retailer, 'mobile_number', '') if device.retailer else ''
            AppleMDMService.take_action(
                device_reg_num=device.provider_device_id or device.serial_number or device.imei,
                action="LOCK",
                message=reason or device.lock_message or "Payment overdue. Please contact admin.",
                phone_number=phone,
                foot_note="Prime Locker Protection"
            )

        # In-app notifications (M9)
        from accounts.views import send_system_notification
        from accounts.models import NotificationType
        send_system_notification(
            recipient=device.retailer,
            title="Device Locked",
            message=f"Device {device.imei} was locked.",
            notification_type=NotificationType.DEVICE_LOCK,
            reference_id=device.id,
            sender=request.user
        )
        send_system_notification(
            recipient=device.customer,
            title="Device Locked",
            message=f"Your device ({device.imei}) has been locked.",
            notification_type=NotificationType.DEVICE_LOCK,
            reference_id=device.id,
            sender=request.user
        )

        AuditLog.objects.create(
            user=request.user,
            action="REMOTE_LOCK_DEVICE",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei, "reason": reason}
        )
        return Response({"message": f"Device {device.imei} locked.", "status": device.status})

    @action(detail=True, methods=['post'])
    def unlock(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device.status = DeviceLockStatus.ACTIVE
        device.save(update_fields=['status'])
        self._create_command(device, CommandType.UNLOCK)

        if device.emi_contract:
            device.emi_contract.status = EMIContractStatus.ACTIVE
            device.emi_contract.save(update_fields=['status'])

        reason = request.data.get('reason', '').strip()

        # Section B: Trigger RocketPay OEM DLC Unlock if device is controlled by RocketPay
        if device.lock_provider == LockProvider.ROCKETPAY:
            from .services.rocketpay_service import RocketPayService
            RocketPayService.unlock_device(device=device, reason=reason or "Remote unlock from dashboard", user=request.user)
        elif device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.unlock_device(device=device, reason=reason or "Remote unlock from dashboard", user=request.user)
        elif device.lock_provider == LockProvider.APPLE_MDM or device.os_type == OSType.IOS:
            from .services.apple_mdm import AppleMDMService
            AppleMDMService.take_action(
                device_reg_num=device.provider_device_id or device.serial_number or device.imei,
                action="UNLOCK"
            )

        # In-app notifications (M9)
        from accounts.views import send_system_notification
        from accounts.models import NotificationType
        send_system_notification(
            recipient=device.retailer,
            title="Device Unlocked",
            message=f"Device {device.imei} was unlocked.",
            notification_type=NotificationType.DEVICE_UNLOCK,
            reference_id=device.id,
            sender=request.user
        )
        send_system_notification(
            recipient=device.customer,
            title="Device Unlocked",
            message=f"Your device ({device.imei}) has been unlocked.",
            notification_type=NotificationType.DEVICE_UNLOCK,
            reference_id=device.id,
            sender=request.user
        )

        AuditLog.objects.create(
            user=request.user,
            action="REMOTE_UNLOCK_DEVICE",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei, "reason": reason}
        )
        return Response({"message": f"Device {device.imei} unlocked.", "status": device.status})

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        """
        Marks device as activated on physical customer handset after QR scan.
        Transitions status from INACTIVE -> ACTIVE, locks customer editing.
        """
        device = self.get_object()
        now = timezone.now()
        device.is_activated = True
        device.activated_at = now
        device.last_online_at = now
        if device.status == DeviceLockStatus.INACTIVE:
            device.status = DeviceLockStatus.ACTIVE
        device.save(update_fields=['is_activated', 'activated_at', 'last_online_at', 'status'])

        AuditLog.objects.create(
            user=request.user,
            action="ACTIVATE_DEVICE",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei}
        )
        return Response({
            "message": f"Device {device.imei} activated successfully.",
            "status": device.status,
            "is_activated": device.is_activated,
            "is_online": device.is_online,
            "activated_at": device.activated_at,
            "last_online_at": device.last_online_at
        })

    @action(detail=True, methods=['post', 'get'], url_path='ping')
    def device_ping(self, request, pk=None):
        """
        Direct Ping endpoint for a specific device: POST /api/devices/{id}/ping/
        Immediately marks device as online and updates telemetry (battery, location, SIM).
        """
        device = self.get_object()
        device.last_online_at = timezone.now()
        device.is_activated = True
        if device.status == DeviceLockStatus.INACTIVE:
            device.status = DeviceLockStatus.ACTIVE

        update_fields = ['last_online_at', 'is_activated', 'status']

        battery = request.data.get('battery_level') or request.query_params.get('battery_level')
        if battery is not None:
            try:
                device.battery_level = int(battery)
                update_fields.append('battery_level')
            except (ValueError, TypeError):
                pass

        # GPS Location telemetry
        lat = request.data.get('latitude') or request.data.get('current_latitude') or request.query_params.get('latitude')
        lng = request.data.get('longitude') or request.data.get('current_longitude') or request.query_params.get('longitude')
        addr = request.data.get('address') or request.data.get('current_address') or request.query_params.get('address')
        if lat and lng:
            try:
                device.current_latitude = float(lat)
                device.current_longitude = float(lng)
                device.current_address = addr
                device.last_location_at = timezone.now()
                update_fields.extend(['current_latitude', 'current_longitude', 'current_address', 'last_location_at'])
                DeviceLocationLog.objects.create(
                    device=device,
                    latitude=float(lat),
                    longitude=float(lng),
                    address=addr
                )
            except (ValueError, TypeError):
                pass

        # SIM Card telemetry
        for sim_f in ['sim_operator', 'sim_number', 'sim_serial', 'sim_operator_2', 'sim_number_2']:
            val = request.data.get(sim_f) or request.query_params.get(sim_f)
            if val:
                setattr(device, sim_f, val)
                update_fields.append(sim_f)

        # Update real hardware model if provided by APK / telemetry
        reported_model = request.data.get('model') or request.data.get('device_model') or request.query_params.get('model')
        reported_brand = request.data.get('brand') or request.query_params.get('brand')
        reported_name = request.data.get('device_name') or request.query_params.get('device_name')
        if reported_model:
            is_generic = (
                not device.model or
                str(device.model).startswith('Model TAC-') or
                str(device.model).startswith('Model 35') or
                device.model in ('Unknown', 'Android Device')
            )
            if is_generic:
                device.model = str(reported_model).strip()
                update_fields.append('model')
                if reported_brand:
                    device.brand = str(reported_brand).strip()
                    update_fields.append('brand')
                device.device_name = str(reported_name).strip() if reported_name else f"{device.brand} {device.model}".strip()
                update_fields.append('device_name')

        device.save(update_fields=list(set(update_fields)))
        return Response({
            "message": f"Device {device.imei} pinged successfully.",
            "is_online": device.is_online,
            "last_online_at": device.last_online_at,
            "battery_level": device.battery_level,
            "status": device.status,
            "brand": device.brand,
            "model": device.model,
            "device_name": device.device_name,
            "current_location": {
                "latitude": device.current_latitude,
                "longitude": device.current_longitude,
                "address": device.current_address,
                "last_location_at": device.last_location_at
            },
            "sim_details": device.get_sim_details()
        })

    @action(detail=True, methods=['post'], url_path='uninstall')
    def uninstall(self, request, pk=None):
        """
        Retailer or Admin uninstalls/releases device after EMI completion.
        Releases remote MDM locks, marks status UNENROLLED / UNREGISTERED.
        Customer details will then display UNINSTALL.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()

        # Check if EMI contract is completed
        from emi.models import CustomerEMIContract, EMIContractStatus
        active_contracts = CustomerEMIContract.objects.filter(
            models.Q(lock_device=device) | models.Q(device_imei=device.imei)
        ).exclude(status=EMIContractStatus.COMPLETED)

        force = str(request.data.get('force', '')).lower() in ['true', '1']
        if active_contracts.exists() and not (force and request.user.role == 'SUPER_ADMIN'):
            pending_ids = list(active_contracts.values_list('id', flat=True))
            return Response(
                {
                    "detail": f"Device cannot be uninstalled because EMI contract(s) {pending_ids} are still active with pending payments. All EMI payments must be completed first.",
                    "active_contracts": pending_ids
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # Call provider unregister/remove if applicable
        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.remove_device(device=device, user=request.user)
        elif device.lock_provider == LockProvider.ROCKETPAY:
            from .services.rocketpay_service import RocketPayService
            RocketPayService.unregister_key(device=device, user=request.user)
        elif device.lock_provider == LockProvider.APPLE_MDM or device.os_type == OSType.IOS:
            from .services.apple_mdm import AppleMDMService
            AppleMDMService.uninstall_device(
                device_reg_num=device.provider_device_id or device.serial_number or device.imei
            )

        device.status = DeviceLockStatus.UNENROLLED
        device.provider_status = 'UNREGISTERED'
        device.save(update_fields=['status', 'provider_status'])

        AuditLog.objects.create(
            user=request.user,
            action="UNINSTALL_DEVICE",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei, "reason": request.data.get('reason', 'Retailer uninstalled')}
        )
        return Response({
            "message": f"Device {device.imei} uninstalled successfully.",
            "status": device.status,
            "customer_status": "UNINSTALL"
        })

    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.reboot_device(device=device, user=request.user)
        self._create_command(device, CommandType.REBOOT)
        return Response({"message": f"Remote reboot command queued for device {device.imei}."})

    @action(detail=True, methods=['post'], url_path='toggle-camera')
    def toggle_camera(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if is_rocketpay_running_phone(device):
            return Response(
                {"error": "Running collateral phones on RocketPay do not support camera controls. Only LOCK, UNLOCK_CODE, AUDIO_WARNING, GET_LOCATION, and UNREGISTER are supported."},
                status=status.HTTP_400_BAD_REQUEST
            )

        new_camera_disabled = not device.camera_disabled
        if device.lock_provider == LockProvider.ROCKETPAY:
            # RP7: camera_disabled -> CAMERA OFF/ON
            action = 'OFF' if new_camera_disabled else 'ON'
            rp_res = RocketPayService.apply_control(device, control='CAMERA', action=action, user=request.user)
            if not rp_res.get('success'):
                return Response({"error": rp_res.get('error')}, status=status.HTTP_400_BAD_REQUEST)

        device.camera_disabled = new_camera_disabled
        device.save(update_fields=['camera_disabled'])
        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            policy_payload = {
                "disable_camera": new_camera_disabled,
                "camera_disabled": new_camera_disabled,
                "camera_enabled": not new_camera_disabled,
            }
            PartnerMDMService.update_policy(device, policy_payload, user=request.user)
        self._create_command(device, CommandType.UPDATE_POLICY, {
            "camera_disabled": device.camera_disabled,
            "disable_camera": device.camera_disabled
        })
        return Response({
            "message": f"Camera is now {'DISABLED' if device.camera_disabled else 'ENABLED'}.",
            "camera_disabled": device.camera_disabled
        })

    @action(detail=True, methods=['post'], url_path='toggle-social-media')
    def toggle_social_media(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if is_rocketpay_running_phone(device):
            return Response(
                {"error": "Running collateral phones on RocketPay do not support social media controls. Only LOCK, UNLOCK_CODE, AUDIO_WARNING, GET_LOCATION, and UNREGISTER are supported."},
                status=status.HTTP_400_BAD_REQUEST
            )

        new_state = not device.social_media_disabled
        if device.lock_provider == LockProvider.ROCKETPAY:
            # RP6: social media = WHATSAPP + INSTAGRAM + FACEBOOK + SNAPCHAT + TELEGRAM (OFF=block, ON=allow)
            action = 'OFF' if new_state else 'ON'
            for app in ['WHATSAPP', 'INSTAGRAM', 'FACEBOOK', 'SNAPCHAT', 'TELEGRAM']:
                RocketPayService.apply_control(device, control=app, action=action, user=request.user)

        device.social_media_disabled = new_state
        device.whatsapp_disabled = device.social_media_disabled
        device.instagram_disabled = device.social_media_disabled
        device.facebook_disabled = device.social_media_disabled
        device.save(update_fields=['social_media_disabled', 'whatsapp_disabled', 'instagram_disabled', 'facebook_disabled'])
        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.update_policy(device, {"social_apps_enabled": not new_state}, user=request.user)
        self._create_command(device, CommandType.UPDATE_POLICY, {"social_media_disabled": device.social_media_disabled})
        return Response({
            "message": f"Social media apps are now {'DISABLED' if device.social_media_disabled else 'ENABLED'}.",
            "social_media_disabled": device.social_media_disabled
        })

    @action(detail=True, methods=['post'], url_path='push-reminder')
    def push_reminder(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        message = request.data.get('message')
        if not message:
            return Response({"detail": "Field 'message' is required."}, status=status.HTTP_400_BAD_REQUEST)
        device = self.get_object()
        device.lock_message = message
        device.save(update_fields=['lock_message'])
        if device.lock_provider == LockProvider.ROCKETPAY:
            # RP9: Forward to TEXT_REMINDER
            RocketPayService.apply_control(device, control='TEXT_REMINDER', action='SEND', payload={"message": message}, user=request.user)
        elif device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.send_notification(device, title="EMI Reminder", message=message, user=request.user)
        self._create_command(device, CommandType.PUSH_MESSAGE, {"message": message})
        return Response({"message": "Screen reminder sent successfully.", "lock_message": device.lock_message})

    @action(detail=True, methods=['get'], url_path='commands')
    def command_history(self, request, pk=None):
        """
        Device Command History (Screen A-12 / Activity Tab - Missing API M4).
        Returns historical log of remote commands sent to this device.
        """
        device = self.get_object()
        commands = device.commands.all()[:100]
        results = [
            {
                "id": c.id,
                "command_type": c.command_type,
                "command_type_display": c.get_command_type_display(),
                "status": c.status,
                "parameters": c.parameters,
                "issued_by": {
                    "id": c.issued_by.id,
                    "username": c.issued_by.username,
                    "role": c.issued_by.role
                } if c.issued_by else None,
                "created_at": c.created_at,
                "executed_at": c.executed_at,
                "error_message": c.error_message
            }
            for c in commands
        ]
        return Response({"device_imei": device.imei, "commands": results})

    @action(detail=True, methods=['post'], url_path='controls')
    def set_controls(self, request, pk=None):
        """
        Set-style controls (Missing API M5): POST /api/devices/:id/controls/
        Accepts explicit boolean dictionary of controls to avoid race conditions:
        {"camera_disabled": true, "block_factory_reset": true, ...}
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        data = request.data

        if device.lock_provider == LockProvider.ROCKETPAY:
            blocked_for_rp = ['social_media_disabled', 'whatsapp_disabled', 'instagram_disabled', 'facebook_disabled', 'streaming_blocked']
            if any(k in data for k in blocked_for_rp):
                return Response(
                    {"error": "App locking, social media switches, and streaming blocks are not supported on RocketPay OEM DLC devices. Use hardware controls (camera, factory reset, etc.) or own-agent MDM."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        control_fields = [
            'camera_disabled', 'block_factory_reset', 'frp_protection_enabled',
            'social_media_disabled', 'whatsapp_disabled', 'instagram_disabled',
            'facebook_disabled', 'wallpaper_locked', 'streaming_blocked'
        ]
        updated = {}
        for field in control_fields:
            if field in data:
                val = bool(data[field])
                setattr(device, field, val)
                updated[field] = val

        if 'lock_message' in data:
            device.lock_message = str(data['lock_message']).strip()
            updated['lock_message'] = device.lock_message

        if updated:
            device.save(update_fields=list(updated.keys()))
            self._create_command(device, CommandType.UPDATE_POLICY, updated)

            AuditLog.objects.create(
                user=request.user,
                action="SET_DEVICE_CONTROLS",
                ip_address=get_client_ip(request),
                details={"device_id": device.id, "imei": device.imei, "updated_controls": updated}
            )

        return Response({
            "message": "Device controls updated successfully.",
            "device_imei": device.imei,
            "controls": {
                "camera_disabled": device.camera_disabled,
                "block_factory_reset": device.block_factory_reset,
                "frp_protection_enabled": device.frp_protection_enabled,
                "social_media_disabled": device.social_media_disabled,
                "whatsapp_disabled": device.whatsapp_disabled,
                "instagram_disabled": device.instagram_disabled,
                "facebook_disabled": device.facebook_disabled,
                "wallpaper_locked": device.wallpaper_locked,
                "streaming_blocked": device.streaming_blocked,
                "lock_message": device.lock_message
            }
        })

    @action(detail=True, methods=['get', 'post'], url_path='generate-offline-code')
    def generate_offline_code_view(self, request, pk=None):
        """
        Generates or retrieves a secure offline unlock code with expiry options (Missing API M7).
        GET returns current active valid code (syncing dynamically with Partner MDM/RocketPay if needed).
        POST generates or refreshes the code.
        expiry_option choices: 'UNTIL_NEXT_DUE_DATE' (default), '24_HOURS', 'PERMANENT', '10_MINUTES'
        For RocketPay DLC devices, supports dynamic challenge/response unlock code.
        For Partner MDM devices, calls dynamic 10-minute code retrieval.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        from .models import generate_offline_code
        from .services.partner_mdm_service import PartnerMDMService
        from .services.rocketpay_service import RocketPayService

        device = self.get_object()

        challenge = (
            request.data.get('code') or
            request.data.get('challenge_code') or
            request.query_params.get('code') or
            request.query_params.get('challenge_code') or
            ""
        ).strip()

        force = request.data.get('force') or request.data.get('regenerate') or request.query_params.get('force') or False
        if isinstance(force, str):
            force = force.lower() in ('true', '1', 'yes')

        is_expired = bool(device.offline_code_expires_at and timezone.now() > device.offline_code_expires_at)

        new_code = None
        expiry_option = None
        expires_at = None
        message = ""

        # Case 1: Partner MDM devices (dynamic 10-minute code)
        if device.lock_provider == LockProvider.PARTNER_MDM:
            mdm_res = PartnerMDMService.get_offline_code(device)
            if mdm_res.get('success') and (mdm_res.get('offline_unlock_code') or mdm_res.get('unlock_code')):
                new_code = mdm_res.get('offline_unlock_code') or mdm_res.get('unlock_code')
                expiry_option = '10_MINUTES'
                expires_at = timezone.now() + timedelta(minutes=10)
                message = "Partner MDM dynamic offline unlock code retrieved (valid for 10 minutes)."
            elif PartnerMDMService.is_simulation_allowed():
                new_code = "235165"
                expiry_option = '10_MINUTES'
                expires_at = timezone.now() + timedelta(minutes=10)
                message = "Partner MDM offline unlock code generated (simulation)."
            elif device.offline_unlock_code and not force and not is_expired:
                new_code = device.offline_unlock_code
                expiry_option = device.offline_code_expiry_type or '10_MINUTES'
                expires_at = device.offline_code_expires_at
                message = "Current offline unlock code retrieved."
            else:
                new_code = generate_offline_code()
                expiry_option = '10_MINUTES'
                expires_at = timezone.now() + timedelta(minutes=10)
                message = "Offline unlock code generated."

        # Case 2: RocketPay DLC devices (challenge-response)
        elif device.lock_provider == LockProvider.ROCKETPAY:
            if challenge:
                rp_res = RocketPayService.generate_offline_code(device, challenge_code=challenge)
                if rp_res.get('success') and rp_res.get('code'):
                    new_code = rp_res.get('code')
                    message = "RocketPay challenge-response offline unlock code generated successfully."
                elif RocketPayService.is_simulation_allowed():
                    import random
                    new_code = f"{random.randint(100000, 999999)}"
                    message = "Simulated RocketPay offline unlock code generated."
                else:
                    return Response(
                        {"error": rp_res.get('error', 'Failed to generate offline unlock code via RocketPay.')},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                if request.method == 'GET' and device.offline_unlock_code and not is_expired:
                    new_code = device.offline_unlock_code
                    expiry_option = device.offline_code_expiry_type or 'UNTIL_NEXT_DUE_DATE'
                    expires_at = device.offline_code_expires_at
                    message = "Offline unlock code retrieved successfully."
                else:
                    rp_res = RocketPayService.generate_offline_code(device, challenge_code="")
                    if rp_res.get('success') and rp_res.get('code'):
                        new_code = rp_res.get('code')
                        message = "RocketPay offline unlock code generated."
                    elif RocketPayService.is_simulation_allowed() or getattr(device, 'offline_unlock_code', None):
                        new_code = device.offline_unlock_code or generate_offline_code()
                        message = "Offline unlock code retrieved."
                    else:
                        return Response(
                            {
                                "error": "Challenge code is required from the locked RocketPay device screen. Pass challenge_code parameter.",
                                "challenge_required": True
                            },
                            status=status.HTTP_400_BAD_REQUEST
                        )

        # Case 3: Own Agent and other devices
        else:
            if request.method == 'GET' and device.offline_unlock_code and not is_expired and not force:
                new_code = device.offline_unlock_code
                expiry_option = device.offline_code_expiry_type or 'UNTIL_NEXT_DUE_DATE'
                expires_at = device.offline_code_expires_at
                message = "Offline unlock code retrieved successfully."
            elif request.method == 'POST' and not force and not is_expired and device.offline_unlock_code and not device.is_online:
                new_code = device.offline_unlock_code
                expiry_option = device.offline_code_expiry_type or 'UNTIL_NEXT_DUE_DATE'
                expires_at = device.offline_code_expires_at
                message = "Active offline unlock code retrieved (device is offline, preserved cached pin)."
            else:
                new_code = generate_offline_code()
                message = "New offline unlock code generated."

        if not expiry_option:
            expiry_option = (request.data.get('expiry_option') if request.method == 'POST' else None) or 'UNTIL_NEXT_DUE_DATE'
            expiry_option = expiry_option.upper()

        if expires_at is None:
            if expiry_option == '24_HOURS':
                expires_at = timezone.now() + timedelta(hours=24)
            elif expiry_option == '10_MINUTES':
                expires_at = timezone.now() + timedelta(minutes=10)
            elif expiry_option == 'PERMANENT':
                expires_at = None
            else:
                expiry_option = 'UNTIL_NEXT_DUE_DATE'
                if device.emi_contract:
                    from emi.models import InstallmentStatus
                    next_inst = device.emi_contract.installments.filter(
                        status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE]
                    ).order_by('due_date').first()
                    if next_inst:
                        from datetime import datetime, time
                        dt = datetime.combine(next_inst.due_date, time(23, 59, 59))
                        expires_at = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
                    else:
                        expires_at = timezone.now() + timedelta(days=30)
                else:
                    expires_at = timezone.now() + timedelta(days=30)

        device.offline_unlock_code = new_code
        device.offline_code_expires_at = expires_at
        device.offline_code_expiry_type = expiry_option
        device.save(update_fields=['offline_unlock_code', 'offline_code_expires_at', 'offline_code_expiry_type'])

        AuditLog.objects.create(
            user=request.user,
            action="GENERATED_OFFLINE_CODE",
            ip_address=get_client_ip(request),
            details={
                "device_id": device.id,
                "imei": device.imei,
                "expiry_option": expiry_option,
                "expires_at": str(expires_at),
                "lock_provider": device.lock_provider,
                "challenge": challenge if challenge else None
            }
        )

        master_code = get_master_offline_code(device.imei)

        return Response({
            "success": True,
            "message": message or "Offline unlock code retrieved successfully.",
            "device_id": device.id,
            "device_imei": device.imei,
            "imei": device.imei,
            "offline_unlock_code": new_code,
            "code": new_code,
            "unlock_code": new_code,
            "master_recovery_code": master_code,
            "expiry_option": expiry_option,
            "expires_at": expires_at,
            "valid_until": "10 minutes" if expiry_option == '10_MINUTES' else (str(expires_at) if expires_at else "Permanent"),
            "lock_provider": device.lock_provider,
            "is_online": device.is_online
        })

    @action(detail=True, methods=['get', 'post'], url_path='offline-unlock-code')
    def offline_unlock_code_view(self, request, pk=None):
        """
        Alias endpoint: GET/POST /api/devices/{id}/offline-unlock-code/
        """
        return self.generate_offline_code_view(request, pk)

    @action(detail=True, methods=['get', 'post'], url_path='offline-code')
    def offline_code_view(self, request, pk=None):
        """
        Alias endpoint: GET/POST /api/devices/{id}/offline-code/
        """
        return self.generate_offline_code_view(request, pk)

    @action(detail=True, methods=['post'], url_path='verify-offline-code')
    def verify_offline_code_view(self, request, pk=None):
        """
        Action: POST /api/devices/{id}/verify-offline-code/
        Validates an entered offline unlock PIN for this device.
        Optionally unlocks device if unlock_device=True or auto_unlock=True.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        input_code = str(
            request.data.get('code') or
            request.data.get('offline_unlock_code') or
            request.data.get('unlock_code') or
            ""
        ).strip()

        if not input_code:
            return Response(
                {"success": False, "valid": False, "error": "Verification code is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        master_code = get_master_offline_code(device.imei)
        is_code_match = (input_code == str(device.offline_unlock_code).strip())
        is_master_match = bool(master_code and input_code == master_code)

        if not (is_code_match or is_master_match):
            return Response({
                "success": False,
                "valid": False,
                "device_id": device.id,
                "imei": device.imei,
                "error": "Invalid offline unlock code."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check expiry if it matched offline_unlock_code (master code never expires)
        if is_code_match and not is_master_match:
            if device.offline_code_expires_at and timezone.now() > device.offline_code_expires_at:
                return Response({
                    "success": False,
                    "valid": False,
                    "device_id": device.id,
                    "imei": device.imei,
                    "error": "Offline unlock code has expired. Please request a new code.",
                    "is_expired": True
                }, status=status.HTTP_400_BAD_REQUEST)

        should_unlock = request.data.get('unlock_device') or request.data.get('auto_unlock')
        if isinstance(should_unlock, str):
            should_unlock = should_unlock.lower() in ('true', '1', 'yes')

        if should_unlock and device.status == DeviceLockStatus.LOCKED:
            device.status = DeviceLockStatus.ACTIVE
            device.save(update_fields=['status'])
            self._create_command(device, CommandType.UNLOCK)
            if device.emi_contract:
                device.emi_contract.status = EMIContractStatus.ACTIVE
                device.emi_contract.save(update_fields=['status'])

            AuditLog.objects.create(
                user=request.user,
                action="OFFLINE_CODE_UNLOCKED",
                ip_address=get_client_ip(request),
                details={
                    "device_id": device.id,
                    "imei": device.imei,
                    "unlocked_via": "MASTER_CODE" if is_master_match else "OFFLINE_PIN"
                }
            )

        return Response({
            "success": True,
            "valid": True,
            "device_id": device.id,
            "imei": device.imei,
            "device_status": device.status,
            "is_master_code": is_master_match,
            "message": "Offline unlock code is valid." + (" Device has been unlocked." if should_unlock else "")
        })

    @action(detail=True, methods=['get'], url_path='apps')
    def get_apps(self, request, pk=None):
        """
        Device Agent Commands (Missing API M10):
        Lists installed apps and currently locked packages on the device.
        """
        device = self.get_object()
        return Response({
            "device_imei": device.imei,
            "installed_apps": device.installed_apps or [],
            "locked_packages": device.locked_packages or []
        })

    @action(detail=True, methods=['post'], url_path='apps/lock')
    def lock_app(self, request, pk=None):
        """
        Per-app lock (Missing API M10 & RP6):
        For RocketPay, maps package to app_control and applies OFF (blocked).
        Running phones on RocketPay do not support app locking.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if is_rocketpay_running_phone(device):
            return Response(
                {"error": "Running collateral phones on RocketPay do not support app locking. Only LOCK, UNLOCK_CODE, AUDIO_WARNING, GET_LOCATION, and UNREGISTER are supported."},
                status=status.HTTP_400_BAD_REQUEST
            )

        package_name = request.data.get('package_name')
        package_names = request.data.get('package_names', [])
        if package_name and package_name not in package_names:
            package_names.append(package_name)

        if not package_names:
            return Response({"detail": "Field 'package_name' or 'package_names' is required."}, status=status.HTTP_400_BAD_REQUEST)

        # RP6: Forward to RocketPay if device is ROCKETPAY
        if device.lock_provider == LockProvider.ROCKETPAY:
            for pkg in package_names:
                app_name = ROCKETPAY_APP_MAP.get(pkg.lower(), pkg.split('.')[-1].upper())
                RocketPayService.apply_control(device, control=app_name, action='OFF', user=request.user)

        current_locked = list(device.locked_packages or [])
        for pkg in package_names:
            if pkg not in current_locked:
                current_locked.append(pkg)

        device.locked_packages = current_locked
        device.save(update_fields=['locked_packages'])
        self._create_command(device, CommandType.UPDATE_POLICY, {"locked_packages": current_locked})

        return Response({
            "message": f"Locked {len(package_names)} package(s).",
            "locked_packages": device.locked_packages
        })

    @action(detail=True, methods=['post'], url_path='apps/unlock')
    def unlock_app(self, request, pk=None):
        """
        Per-app unlock (Missing API M10 & RP6):
        For RocketPay, maps package to app_control and applies ON (allowed).
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if is_rocketpay_running_phone(device):
            return Response(
                {"error": "Running collateral phones on RocketPay do not support app controls."},
                status=status.HTTP_400_BAD_REQUEST
            )

        package_name = request.data.get('package_name')
        if not package_name:
            return Response({"detail": "Field 'package_name' is required."}, status=status.HTTP_400_BAD_REQUEST)

        # RP6: Forward to RocketPay if device is ROCKETPAY
        if device.lock_provider == LockProvider.ROCKETPAY:
            app_name = ROCKETPAY_APP_MAP.get(package_name.lower(), package_name.split('.')[-1].upper())
            RocketPayService.apply_control(device, control=app_name, action='ON', user=request.user)

        current_locked = [pkg for pkg in (device.locked_packages or []) if pkg != package_name]
        device.locked_packages = current_locked
        device.save(update_fields=['locked_packages'])
        self._create_command(device, CommandType.UPDATE_POLICY, {"locked_packages": current_locked})

        return Response({
            "message": f"Unlocked package '{package_name}'.",
            "locked_packages": device.locked_packages
        })

    @action(detail=True, methods=['post'], url_path='audio-reminder')
    def audio_reminder(self, request, pk=None):
        """
        Audio Reminder (Missing API M10 & RP7): plays sound/voice reminder on the phone/TV.
        For RocketPay: calls AUDIO_WARNING apply SEND.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        message = request.data.get('message', 'Please pay your EMI to avoid device locking.')
        volume = int(request.data.get('volume', 100))

        if device.lock_provider == LockProvider.ROCKETPAY:
            RocketPayService.apply_control(device, control='AUDIO_WARNING', action='SEND', payload={"message": message, "volume": volume}, user=request.user)
        elif device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.audio_reminder(device, user=request.user)

        self._create_command(device, CommandType.PUSH_MESSAGE, {
            "action": "AUDIO_REMINDER",
            "message": message,
            "volume": volume
        })
        return Response({"message": f"Audio reminder command dispatched to device {device.imei}."})

    @action(detail=True, methods=['post'], url_path='block-streaming')
    def block_streaming(self, request, pk=None):
        """
        TV/Mobile Streaming app block (Missing API M10 & RP6):
        Blocks/unblocks YouTube, Hotstar, Prime Video, Jio Cinema.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if is_rocketpay_running_phone(device):
            return Response(
                {"error": "Running collateral phones on RocketPay do not support streaming app blocking."},
                status=status.HTTP_400_BAD_REQUEST
            )

        streaming_blocked = request.data.get('streaming_blocked')
        if streaming_blocked is None:
            streaming_blocked = not device.streaming_blocked

        # RP6: Forward to RocketPay streaming apps
        if device.lock_provider == LockProvider.ROCKETPAY:
            action = 'OFF' if streaming_blocked else 'ON'
            for app in ['YOUTUBE', 'HOTSTAR', 'AMAZON_PRIME', 'JIO_CINEMA']:
                RocketPayService.apply_control(device, control=app, action=action, user=request.user)

        device.streaming_blocked = bool(streaming_blocked)
        device.save(update_fields=['streaming_blocked'])
        self._create_command(device, CommandType.UPDATE_POLICY, {"streaming_blocked": device.streaming_blocked})

        return Response({
            "message": f"Streaming apps are now {'BLOCKED' if device.streaming_blocked else 'UNBLOCKED'}.",
            "streaming_blocked": device.streaming_blocked
        })

    @action(detail=True, methods=['post'], url_path='refresh-location')
    def refresh_location(self, request, pk=None):
        """
        Refresh Location Now (Missing API M10 & RP7): immediately requests live GPS sync from device agent or RocketPay GET_LOCATION FETCH.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if device.lock_provider == LockProvider.ROCKETPAY:
            RocketPayService.apply_control(device, control='GET_LOCATION', action='FETCH', user=request.user)
        elif device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.refresh_location(device, user=request.user)
        elif device.lock_provider == LockProvider.APPLE_MDM or device.os_type == OSType.IOS:
            from .services.apple_mdm import AppleMDMService
            AppleMDMService.get_location(
                device_reg_num=device.provider_device_id or device.serial_number or device.imei
            )

        self._create_command(device, CommandType.SYNC_LOCATION)
        return Response({"message": f"Live GPS location refresh command dispatched to device {device.imei}."})

    @action(detail=True, methods=['get'], url_path='location-history')
    def location_history(self, request, pk=None):
        device = self.get_object()
        logs = device.location_history.all()[:100]
        serializer = DeviceLocationLogSerializer(logs, many=True)
        return Response({
            "device_imei": device.imei,
            "current_location": {
                "latitude": device.current_latitude,
                "longitude": device.current_longitude,
                "address": device.current_address,
                "last_location_at": device.last_location_at
            },
            "history": serializer.data
        })

    @action(detail=True, methods=['get'], url_path='location')
    def device_location(self, request, pk=None):
        """
        Returns the current live GPS coordinates of the device.
        Endpoint: GET /api/devices/{id}/location/
        """
        device = self.get_object()
        # If partner MDM device and local coordinates are not yet recorded, fetch from partner MDM
        if (device.current_latitude is None or device.current_longitude is None) and device.lock_provider == LockProvider.PARTNER_MDM:
            try:
                from .services.partner_mdm_service import PartnerMDMService
                loc_res = PartnerMDMService.get_location(device)
                if loc_res.get('success') and loc_res.get('latitude') is not None:
                    device.current_latitude = loc_res.get('latitude')
                    device.current_longitude = loc_res.get('longitude')
                    device.last_location_at = timezone.now()
                    device.save(update_fields=['current_latitude', 'current_longitude', 'last_location_at'])
            except Exception as e:
                logger.warning("Failed to fetch live location from partner MDM: %s", e)

        lat = float(device.current_latitude) if device.current_latitude is not None else None
        lng = float(device.current_longitude) if device.current_longitude is not None else None
        return Response({
            "imei": device.imei,
            "latitude": lat,
            "longitude": lng,
            "address": device.current_address,
            "last_location_at": device.last_location_at,
            "maps_url": f"https://maps.google.com/?q={lat},{lng}" if lat is not None and lng is not None else None,
            "is_available": bool(lat is not None and lng is not None)
        })

    @action(detail=True, methods=['post'], url_path='toggle-reset-block')
    def toggle_reset_block(self, request, pk=None):
        """
        Reset Option Block & FRP Protection (RP7):
        Remotely blocks/unblocks factory data reset (HARD_RESET OFF/ON on RocketPay).
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if is_rocketpay_running_phone(device):
            return Response(
                {"error": "Running collateral phones on RocketPay do not support factory-reset blocking. Only LOCK, UNLOCK_CODE, AUDIO_WARNING, GET_LOCATION, and UNREGISTER are supported."},
                status=status.HTTP_400_BAD_REQUEST
            )

        new_state = not device.block_factory_reset
        if device.lock_provider == LockProvider.ROCKETPAY:
            # RP7: block_factory_reset -> HARD_RESET OFF/ON
            action = 'OFF' if new_state else 'ON'
            rp_res = RocketPayService.apply_control(device, control='HARD_RESET', action=action, user=request.user)
            if not rp_res.get('success'):
                return Response({"error": rp_res.get('error')}, status=status.HTTP_400_BAD_REQUEST)

        device.block_factory_reset = new_state
        device.frp_protection_enabled = device.block_factory_reset
        device.save(update_fields=['block_factory_reset', 'frp_protection_enabled'])
        self._create_command(device, CommandType.UPDATE_POLICY, {
            "block_factory_reset": device.block_factory_reset,
            "frp_protection_enabled": device.frp_protection_enabled
        })
        AuditLog.objects.create(
            user=request.user,
            action="TOGGLE_RESET_BLOCK",
            ip_address=get_client_ip(request),
            details={
                "device_id": device.id,
                "block_factory_reset": device.block_factory_reset,
                "frp_protection_enabled": device.frp_protection_enabled
            }
        )
        return Response({
            "message": f"Factory reset & FRP protection is now {'ENABLED (BLOCKED)' if device.block_factory_reset else 'DISABLED (ALLOWED)'}.",
            "block_factory_reset": device.block_factory_reset,
            "frp_protection_enabled": device.frp_protection_enabled
        })

    @action(detail=True, methods=['post'], url_path='reset-enrollment')
    def reset_enrollment(self, request, pk=None):
        """
        Re-enroll device keeping the same customer.
        Endpoint: POST /api/devices/{id}/reset-enrollment/
        Resets activation state (is_activated=False, status=INACTIVE, clears FCM token and last_online_at),
        re-triggers MDM provisioning with Partner MDM / RocketPay / Apple MDM,
        regenerates QR codes, and returns the device ready for immediate re-scanning.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()

        customer = device.customer
        if not customer:
            return Response(
                {"error": "This device has no assigned customer to re-enroll."},
                status=status.HTTP_400_BAD_REQUEST
            )

        cust_name = getattr(customer, 'get_full_name', lambda: '')() or customer.first_name or customer.username
        cust_phone = getattr(customer, 'mobile_number', '') or ''
        cust_address = getattr(customer, 'address', '') or ''

        # 1. Reset device activation and hardware tracking fields
        device.status = DeviceLockStatus.INACTIVE
        device.is_activated = False
        device.activated_at = None
        device.last_online_at = None
        device.fcm_token = ""
        device.ip_address = ""
        device.provider_status = "PENDING"
        device.block_factory_reset = False
        device.frp_protection_enabled = False

        # 2. Trigger MDM re-enrollment / refresh QR codes
        qr_code_data = None
        qr_codes_data = {}
        provider_device_id = device.provider_device_id

        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            loan_data = {}
            if hasattr(device, 'emi_contract') and device.emi_contract:
                contract = device.emi_contract
                loan_data = {
                    'device_price': float(contract.total_amount or 15000),
                    'down_payment': float(contract.down_payment or 0),
                    'processing_fee': float(contract.processing_fee or 0),
                    'number_of_emi': int(contract.total_emis or 6),
                    'monthly_emi': float(contract.monthly_emi or 2000),
                    'first_emi_date': str(contract.start_date or timezone.localdate())
                }
            pm_res = PartnerMDMService.enroll_device(
                device=device,
                customer_name=cust_name,
                customer_mobile=cust_phone,
                customer_address=cust_address,
                loan_data=loan_data,
                user=request.user
            )
            if pm_res.get('success'):
                provider_device_id = str(pm_res.get('provider_device_id') or provider_device_id)
                qr_code_data = pm_res.get('qr_code_url')
                qr_codes_data = pm_res.get('qr_codes') or {}
                device.provider_device_id = provider_device_id
                device.provider_status = pm_res.get('provider_status', 'PENDING')
                if qr_code_data:
                    device.qr_code_url = qr_code_data
                if qr_codes_data:
                    device.qr_codes = qr_codes_data
            else:
                generic_qrs = PartnerMDMService.get_generic_enrollment_qrs(user=request.user)
                qr_code_data = generic_qrs.get('qr_service_app') or device.qr_code_url
                qr_codes_data = {
                    'qr_service_app': generic_qrs.get('qr_service_app'),
                    'qr_running_device': generic_qrs.get('qr_running_device'),
                    'qr_wireless': generic_qrs.get('qr_wireless')
                }
                if qr_code_data:
                    device.qr_code_url = qr_code_data
                device.qr_codes = qr_codes_data

        elif device.lock_provider == LockProvider.ROCKETPAY:
            from .services.rocketpay_service import RocketPayService
            mdm_type = "DLC" if device.device_state != DeviceScannerState.RUNNING else "DPC"
            rp_res = RocketPayService.enroll_device(
                device=device,
                customer_name=cust_name,
                customer_mobile=cust_phone,
                retailer_name=getattr(request.user, 'business_name', '') or request.user.username,
                user=request.user,
                mdm_type=mdm_type,
                frp_email=getattr(customer, 'google_account_email', '') or '',
                is_running=(device.device_state == DeviceScannerState.RUNNING)
            )
            if rp_res.get('success'):
                provider_device_id = str(rp_res.get('provider_device_id') or provider_device_id)
                qr_code_data = rp_res.get('qr_code_data')
                device.provider_device_id = provider_device_id
                device.provider_status = rp_res.get('provider_status', 'PENDING')
                if qr_code_data:
                    device.qr_code_url = qr_code_data
                    device.qr_codes = {"qr_service_app": qr_code_data}
                    qr_codes_data = device.qr_codes

        elif device.lock_provider == LockProvider.APPLE_MDM:
            from .services.apple_mdm import AppleMDMService
            apple_res = AppleMDMService.add_device(
                imei=device.imei,
                serial_number=device.serial_number or device.imei,
                customer_name=cust_name,
                customer_mobile=cust_phone,
                customer_email=getattr(customer, 'email', '') or '',
                imei2=device.secondary_imei or ''
            )
            device_reg_num = (apple_res.get('data') or {}).get('deviceRegNum') or ''
            if device_reg_num:
                device.provider_device_id = str(device_reg_num)
            device.provider_status = 'PENDING'

        device.save(update_fields=[
            'status', 'is_activated', 'activated_at', 'last_online_at',
            'fcm_token', 'ip_address', 'provider_status', 'provider_device_id',
            'qr_code_url', 'qr_codes', 'block_factory_reset', 'frp_protection_enabled'
        ])

        AuditLog.objects.create(
            user=request.user,
            action="DEVICE_RESET_FOR_REENROLLMENT",
            ip_address=get_client_ip(request),
            details={
                "device_id": device.id,
                "imei": device.imei,
                "customer_id": customer.id,
                "customer_name": cust_name,
                "lock_provider": device.lock_provider,
                "provider_device_id": device.provider_device_id,
                "status": device.status
            }
        )

        return Response({
            "status": "success",
            "message": f"Device {device.imei} enrollment reset successfully for customer {cust_name}. Ready for re-enrollment.",
            "device": {
                "id": device.id,
                "imei": device.imei,
                "secondary_imei": device.secondary_imei,
                "brand": device.brand,
                "model": device.model,
                "device_name": device.device_name,
                "status": device.status,
                "is_activated": device.is_activated,
                "is_online": device.is_online,
                "provider_status": device.provider_status,
                "lock_provider": device.lock_provider,
                "qr_code_url": device.qr_code_url,
                "qr_codes": device.qr_codes or {},
                "qr_service_app": (device.qr_codes or {}).get("qr_service_app") or device.qr_code_url,
                "qr_running_device": (device.qr_codes or {}).get("qr_running_device"),
                "offline_unlock_code": device.offline_unlock_code
            },
            "customer": {
                "id": customer.id,
                "name": cust_name,
                "phone": cust_phone
            }
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='toggle-hide-app')
    def toggle_hide_app(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        new_val = not device.hide_application
        device.hide_application = new_val
        device.save(update_fields=['hide_application'])
        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            PartnerMDMService.update_policy(device, {"hide_application": new_val}, user=request.user)
        return Response({
            "message": f"Control app is now {'HIDDEN' if device.hide_application else 'VISIBLE'} on device.",
            "hide_application": device.hide_application
        })

    @action(detail=True, methods=['get'], url_path='controls-status')
    def controls_status(self, request, pk=None):
        """
        Read Current Control States (RP8):
        GET /api/devices/:id/controls-status/
        For RocketPay, calls GET /superkey/keys/:key_id/controls/status to return real switch states.
        """
        device = self.get_object()
        if device.lock_provider == LockProvider.ROCKETPAY:
            return Response(RocketPayService.get_controls_status(device))
        return Response({
            "camera_disabled": device.camera_disabled,
            "block_factory_reset": device.block_factory_reset,
            "social_media_disabled": device.social_media_disabled,
            "streaming_blocked": device.streaming_blocked,
            "wallpaper_locked": device.wallpaper_locked
        })

    @action(detail=True, methods=['post'], url_path='set-wallpaper')
    def set_wallpaper(self, request, pk=None):
        """
        Wallpaper Control (RP9):
        Changes, locks, or customizes the lock wallpaper shown on the locked device.
        RocketPay OEM DLC devices do not support wallpaper control (returns 400).
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        if device.lock_provider == LockProvider.ROCKETPAY:
            return Response(
                {"error": "Wallpaper control is not supported on RocketPay OEM DLC devices."},
                status=status.HTTP_400_BAD_REQUEST
            )
        wallpaper_url = request.data.get('wallpaper_url')
        lock_wallpaper = request.data.get('lock_wallpaper', True)

        if 'wallpaper_file' in request.FILES:
            device.custom_wallpaper = request.FILES['wallpaper_file']
            device.save(update_fields=['custom_wallpaper'])
            wallpaper_url = request.build_absolute_uri(device.custom_wallpaper.url)

        if wallpaper_url:
            device.custom_wallpaper_url = wallpaper_url

        device.wallpaper_locked = bool(lock_wallpaper)
        device.save(update_fields=['custom_wallpaper_url', 'wallpaper_locked'])
        self._create_command(device, CommandType.SET_WALLPAPER, {
            "wallpaper_url": device.custom_wallpaper_url or wallpaper_url,
            "wallpaper_locked": device.wallpaper_locked
        })
        AuditLog.objects.create(
            user=request.user,
            action="SET_DEVICE_WALLPAPER",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "wallpaper_url": device.custom_wallpaper_url}
        )
        return Response({
            "message": "Custom lock wallpaper updated and queued for device.",
            "wallpaper_url": device.custom_wallpaper_url,
            "wallpaper_locked": device.wallpaper_locked
        })

    @action(detail=True, methods=['get'], url_path='contacts')
    def get_contacts(self, request, pk=None):
        """
        Get Contact Details:
        Securely retrieves the registered device owner's contact details and synced address book.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        customer = device.customer
        name = f"{customer.first_name} {customer.last_name}".strip() or customer.username

        return Response({
            "device_id": device.id,
            "device_imei": device.imei,
            "registered_owner": {
                "name": name,
                "primary_phone": customer.mobile_number,
                "alternate_phone": customer.alternate_mobile_number or "N/A",
                "email": customer.email,
                "address": customer.address,
                "city": customer.city,
                "state": customer.state,
                "pincode": customer.pincode
            },
            "synced_device_contacts": device.synced_contacts or []
        })

    @action(detail=True, methods=['post'], url_path='acknowledge-sim-change')
    def acknowledge_sim_change(self, request, pk=None):
        """
        Admin or Retailer acknowledges and clears the SIM change warning flag for a device.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device.sim_changed = False
        device.save(update_fields=['sim_changed'])

        AuditLog.objects.create(
            user=request.user,
            action="SIM_CHANGE_ACKNOWLEDGED",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei}
        )
        return Response({
            "message": f"SIM change alert for device {device.imei} has been cleared.",
            "sim_changed": False,
            "sim_details": device.get_sim_details()
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='sim-details')
    def sim_details_view(self, request, pk=None):
        """
        Returns full dual-SIM details, carrier info, ICCID, and swap history.
        Endpoint: GET /api/devices/{id}/sim-details/
        """
        device = self.get_object()
        return Response(device.get_sim_details(), status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='sim')
    def sim_view(self, request, pk=None):
        """
        Alias endpoint: GET /api/devices/{id}/sim/
        """
        return self.sim_details_view(request, pk)

    @action(detail=True, methods=['post'], url_path='attach-emi')
    def attach_emi(self, request, pk=None):
        """
        Step 2: Attach or Configure an EMI Plan for an enrolled Device.
        Supports:
        A) Creating a new EMI contract directly from a Master Plan Template:
           - plan_template_id, total_amount, down_payment (optional), first_emi_date (optional)
        B) Creating a new EMI contract with Custom Financial Terms:
           - total_amount, down_payment, tenure_months, processing_fee (optional), first_emi_date (optional)
        C) Linking an existing contract via emi_contract_id.
        """
        denied = self._block_customer(request)
        if denied:
            return denied

        device = self.get_object()
        user = request.user
        data = request.data

        # Option C: Linking existing contract
        contract_id = data.get('emi_contract_id')
        if contract_id:
            contract = CustomerEMIContract.objects.filter(id=contract_id).first()
            if not contract:
                return Response({"detail": "EMI contract not found."}, status=status.HTTP_404_NOT_FOUND)
            device.emi_contract = contract
            device.save(update_fields=['emi_contract'])
            return Response({
                "message": f"EMI contract #{contract.id} successfully attached to device {device.imei}.",
                "device_id": device.id,
                "contract_id": contract.id,
                "contract": {
                    "id": contract.id,
                    "device_imei": contract.device_imei,
                    "total_amount": contract.total_amount,
                    "financed_amount": contract.financed_amount,
                    "monthly_emi_amount": contract.monthly_emi_amount,
                    "tenure_months": contract.tenure_months,
                    "status": contract.status
                }
            })

        # Options A & B: Configure and create new EMI contract directly on this device
        total_amount = data.get('total_amount')
        if not total_amount:
            return Response(
                {"detail": "Either 'emi_contract_id' or 'total_amount' is required to attach an EMI plan."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            tot = Decimal(str(total_amount))
            down = Decimal(str(data.get('down_payment', 0.00)))
        except Exception:
            return Response({"detail": "Invalid total_amount or down_payment format."}, status=status.HTTP_400_BAD_REQUEST)

        if down > tot:
            return Response({"detail": "Down payment cannot exceed total device price."}, status=status.HTTP_400_BAD_REQUEST)

        plan_template_id = data.get('plan_template_id')
        plan_template = None
        interest_pct = Decimal('0.00')
        proc_fee = Decimal(str(data.get('processing_fee', 0.00)))
        tenure = int(data.get('tenure_months', 12)) if data.get('tenure_months') else 12

        if plan_template_id:
            plan_template = EMIPlanTemplate.objects.filter(id=plan_template_id, is_active=True).first()
            if not plan_template:
                return Response({"detail": f"EMI Plan Template #{plan_template_id} not found or inactive."}, status=status.HTTP_404_NOT_FOUND)
            tenure = int(data.get('tenure_months') or plan_template.tenure_months)
            proc_fee = Decimal(str(data.get('processing_fee') or plan_template.processing_fee))
            interest_pct = plan_template.interest_rate_percentage

        principal = tot - down + proc_fee
        interest_amount = (principal * interest_pct) / Decimal('100.00')
        financed = round(principal + interest_amount, 2)
        monthly_emi = round(financed / Decimal(tenure), 2)

        first_date_str = data.get('first_emi_date')
        first_date = timezone.datetime.strptime(first_date_str, '%Y-%m-%d').date() if first_date_str else timezone.localdate()

        with transaction.atomic():
            contract = CustomerEMIContract.objects.create(
                customer=device.customer,
                retailer=device.retailer or user,
                plan_template=plan_template,
                device_imei=device.imei,
                device_name=device.device_name,
                device_type=device.device_category,
                device_state=device.device_state,
                device_brand=device.brand,
                device_model=device.model,
                loan_provider=device.loan_provider,
                loan_provider_other=device.loan_provider_other,
                loan_account_number=device.loan_account_number,
                total_amount=tot,
                down_payment=down,
                processing_fee=proc_fee,
                financed_amount=financed,
                tenure_months=tenure,
                monthly_emi_amount=monthly_emi,
                first_emi_date=first_date,
                status=EMIContractStatus.ACTIVE
            )

            installments = [
                EMIInstallment(
                    contract=contract,
                    installment_number=i,
                    due_date=add_months(first_date, i - 1),
                    amount=monthly_emi,
                    status=InstallmentStatus.UPCOMING
                ) for i in range(1, tenure + 1)
            ]
            EMIInstallment.objects.bulk_create(installments)
            device.emi_contract = contract
            device.save(update_fields=['emi_contract'])

            AuditLog.objects.create(
                user=user,
                action="ATTACHED_EMI_PLAN",
                ip_address=get_client_ip(request),
                details={
                    "device_id": device.id,
                    "contract_id": contract.id,
                    "plan_template_id": plan_template.id if plan_template else None,
                    "financed_amount": str(financed)
                }
            )

        return Response({
            "message": f"EMI plan successfully configured and attached to device {device.imei}.",
            "device_id": device.id,
            "device_imei": device.imei,
            "contract": {
                "id": contract.id,
                "plan_template_id": plan_template.id if plan_template else None,
                "plan_template_name": plan_template.name if plan_template else "Custom Plan",
                "total_amount": tot,
                "down_payment": down,
                "processing_fee": proc_fee,
                "financed_amount": financed,
                "tenure_months": tenure,
                "monthly_emi_amount": monthly_emi,
                "first_emi_date": first_date,
                "installments_count": tenure,
                "status": contract.status
            }
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def alerts(self, request):
        """
        Status Board Device Alerts (Item 16: GET /api/devices/alerts/):
        Surfaces critical operational issues:
        1. Tamper alerts (from RocketPay webhook)
        2. Offline devices (no ping/heartbeat for more than X days, default 3)
        3. Failed RocketPay commands (provider_status='FAILED')
        """
        denied = self._block_customer(request)
        if denied:
            return denied

        qs = self.get_queryset()
        days = int(request.query_params.get('days', 3))
        cutoff = timezone.now() - timedelta(days=days)

        # 1. Tamper alerts
        tamper_devices = qs.filter(
            models.Q(provider_status='TAMPERED') |
            models.Q(provider_error__icontains='tamper')
        ).select_related('customer', 'retailer')

        # 2. Offline alerts (active devices not seen in > X days)
        offline_devices = qs.filter(
            models.Q(last_online_at__lt=cutoff) |
            models.Q(last_online_at__isnull=True, status=DeviceLockStatus.ACTIVE)
        ).select_related('customer', 'retailer')

        # 3. Failed RocketPay commands
        failed_devices = qs.filter(
            provider_status='FAILED'
        ).select_related('customer', 'retailer')

        alerts_list = []
        for d in tamper_devices:
            c_name = f"{d.customer.first_name} {d.customer.last_name}".strip() or d.customer.username
            r_name = f"{d.retailer.first_name} {d.retailer.last_name}".strip() or d.retailer.username
            alerts_list.append({
                "id": f"tamper_{d.id}",
                "type": "TAMPER",
                "severity": "CRITICAL",
                "device_id": d.id,
                "imei": d.imei,
                "brand": d.brand,
                "model": d.model,
                "customer_name": c_name,
                "customer_phone": d.customer.mobile_number,
                "retailer_name": r_name,
                "retailer_store": d.retailer.business_name,
                "message": "Device tamper event reported by RocketPay OEM engine.",
                "timestamp": d.updated_at,
                "action_required": "Investigate physical device or call customer."
            })

        for d in failed_devices:
            c_name = f"{d.customer.first_name} {d.customer.last_name}".strip() or d.customer.username
            r_name = f"{d.retailer.first_name} {d.retailer.last_name}".strip() or d.retailer.username
            alerts_list.append({
                "id": f"failed_{d.id}",
                "type": "FAILED_COMMAND",
                "severity": "HIGH",
                "device_id": d.id,
                "imei": d.imei,
                "brand": d.brand,
                "model": d.model,
                "customer_name": c_name,
                "customer_phone": d.customer.mobile_number,
                "retailer_name": r_name,
                "retailer_store": d.retailer.business_name,
                "message": f"RocketPay command failed: {d.provider_error or 'Unknown provider error'}",
                "timestamp": d.updated_at,
                "action_required": "Click retry button or run retry-provider-command."
            })

        for d in offline_devices:
            c_name = f"{d.customer.first_name} {d.customer.last_name}".strip() or d.customer.username
            r_name = f"{d.retailer.first_name} {d.retailer.last_name}".strip() or d.retailer.username
            alerts_list.append({
                "id": f"offline_{d.id}",
                "type": "OFFLINE",
                "severity": "MEDIUM",
                "device_id": d.id,
                "imei": d.imei,
                "brand": d.brand,
                "model": d.model,
                "customer_name": c_name,
                "customer_phone": d.customer.mobile_number,
                "retailer_name": r_name,
                "retailer_store": d.retailer.business_name,
                "message": f"Device has been offline for more than {days} days.",
                "timestamp": d.last_online_at or d.created_at,
                "action_required": "Ask customer to connect device to internet."
            })

        return Response({
            "summary": {
                "total_alerts": len(alerts_list),
                "tamper_count": tamper_devices.count(),
                "failed_commands_count": failed_devices.count(),
                "offline_count": offline_devices.count()
            },
            "alerts": alerts_list
        })

    @action(detail=True, methods=['post'], url_path='send-offline-code')
    def send_offline_code(self, request, pk=None):
        """
        Sends the emergency offline unlock code to the retailer (Item 17 & RP1).
        For RocketPay DLC devices, supports dynamic challenge/response UNLOCK_CODE.
        For Partner MDM devices, fetches dynamic 10-minute code.
        Dispatches via in-app notification and WhatsApp template.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        code = device.offline_unlock_code

        challenge = (
            request.data.get('code') or
            request.data.get('challenge_code') or
            request.query_params.get('code') or
            request.query_params.get('challenge_code') or
            ""
        ).strip()

        # 1. Partner MDM dynamic code
        if device.lock_provider == LockProvider.PARTNER_MDM:
            from .services.partner_mdm_service import PartnerMDMService
            mdm_res = PartnerMDMService.get_offline_code(device)
            if mdm_res.get('success') and (mdm_res.get('offline_unlock_code') or mdm_res.get('unlock_code')):
                code = mdm_res.get('offline_unlock_code') or mdm_res.get('unlock_code')
                device.offline_unlock_code = code
                device.offline_code_expires_at = timezone.now() + timedelta(minutes=10)
                device.offline_code_expiry_type = '10_MINUTES'
                device.save(update_fields=['offline_unlock_code', 'offline_code_expires_at', 'offline_code_expiry_type'])

        # 2. RocketPay DLC challenge/response
        elif device.lock_provider == LockProvider.ROCKETPAY:
            rp_res = RocketPayService.generate_offline_code(device, challenge_code=challenge)
            if rp_res.get('success') and rp_res.get('code'):
                code = rp_res.get('code')
                device.offline_unlock_code = code
                device.save(update_fields=['offline_unlock_code'])
            elif not code:
                from .models import generate_offline_code
                code = generate_offline_code()
                device.offline_unlock_code = code
                device.save(update_fields=['offline_unlock_code'])

        # 3. Own Agent
        elif not code:
            from .models import generate_offline_code
            code = generate_offline_code()
            device.offline_unlock_code = code
            device.save(update_fields=['offline_unlock_code'])

        from accounts.views import send_system_notification
        from accounts.models import NotificationType
        send_system_notification(
            recipient=device.retailer,
            title="Emergency Offline Unlock Code",
            message=f"Offline unlock code for device {device.imei} ({device.brand} {device.model}): {code}",
            notification_type=NotificationType.DEVICE_UNLOCK,
            reference_id=device.id,
            sender=request.user
        )

        whatsapp_sent = False
        try:
            from accounts.whatsapp import WhatsAppService
            if device.retailer.mobile_number:
                WhatsAppService.send_template(
                    phone_number=device.retailer.mobile_number,
                    template_name='offline_unlock_code',
                    parameters={'code': code, 'imei': device.imei},
                    recipient_name=device.retailer.first_name or device.retailer.username
                )
                whatsapp_sent = True
        except Exception:
            pass

        return Response({
            "success": True,
            "message": f"Offline unlock code sent to retailer {device.retailer.username}.",
            "device_id": device.id,
            "device_imei": device.imei,
            "imei": device.imei,
            "code": code,
            "offline_unlock_code": code,
            "unlock_code": code,
            "in_app_notification_dispatched": True,
            "whatsapp_dispatched": whatsapp_sent
        })

    @action(detail=True, methods=['post'], url_path='retry-provider-command')
    def retry_provider_command(self, request, pk=None):
        """
        Retries a failed RocketPay command on this device (Item 5).
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        from .services.rocketpay_service import RocketPayService

        if device.status == DeviceLockStatus.LOCKED:
            res = RocketPayService.lock_device(device=device, reason="Manual retry of failed lock", user=request.user)
        else:
            res = RocketPayService.unlock_device(device=device, reason="Manual retry of failed unlock", user=request.user)

        if res.get('success'):
            return Response({
                "success": True,
                "message": "Command retried successfully with lock provider.",
                "provider_status": device.provider_status
            })
        return Response({
            "success": False,
            "error": res.get('error', 'Provider retry command failed.'),
            "provider_status": device.provider_status,
            "provider_error": device.provider_error
        }, status=status.HTTP_400_BAD_REQUEST)

    # =========================================================================
    # Apple MDM / Supervised DEP Suite (Loksiz v1.4)
    # =========================================================================

    @action(detail=False, methods=['get'], url_path='apple-mdm/health')
    def apple_mdm_health(self, request):
        """GET /api/devices/apple-mdm/health/ - Verify Apple MDM Gateway connectivity."""
        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.health_check()
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=['post'], url_path='apple-mdm/validate')
    def validate_apple_device(self, request):
        """
        POST /api/devices/apple-mdm/validate/
        Mandatory pre-check to validate iPhone eligibility with Apple MDM before enrollment.
        Checks IMEI, Serial Number, and partner IOS key wallet balance.
        """
        denied = self._block_customer(request)
        if denied:
            return denied

        imei = request.data.get('imei')
        serial_number = request.data.get('serial_number') or request.data.get('serialNumber')
        imei2 = request.data.get('imei2')

        if not imei or not serial_number:
            return Response(
                {"error": 1, "msg": "Both 'imei' (15 digits) and 'serial_number' are required for Apple MDM validation."},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.validate_device(imei=imei, serial_number=serial_number, imei2=imei2)
        http_code = status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST
        return Response(res, status=http_code)

    @action(detail=False, methods=['post'], url_path='apple-mdm/enroll')
    def enroll_apple_device(self, request):
        """
        POST /api/devices/apple-mdm/enroll/
        Enrolls an iPhone into Apple MDM via Loksiz v1.4.
        Generates deviceRegNum and updates or creates the local Device record.
        """
        denied = self._block_customer(request)
        if denied:
            return denied

        imei = request.data.get('imei')
        serial_number = request.data.get('serial_number') or request.data.get('serialNumber')
        customer_name = request.data.get('customer_name') or request.data.get('customerName', '')
        customer_mobile = request.data.get('customer_mobile') or request.data.get('customerMobile', '')
        customer_email = request.data.get('customer_email') or request.data.get('customerEmail', '')
        imei2 = request.data.get('imei2')
        device_id = request.data.get('device_id')

        if not imei or not serial_number:
            return Response(
                {"error": 1, "msg": "Both 'imei' and 'serial_number' are required to enroll an iPhone."},
                status=status.HTTP_400_BAD_REQUEST
            )

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.add_device(
            imei=imei,
            serial_number=serial_number,
            customer_name=customer_name,
            customer_mobile=customer_mobile,
            customer_email=customer_email,
            imei2=imei2
        )

        if res.get('error') != 0:
            return Response(res, status=status.HTTP_400_BAD_REQUEST)

        device_reg_num = (res.get('data') or {}).get('deviceRegNum') or ''

        # Locate or associate local Device record
        device = None
        if device_id:
            device = Device.objects.filter(id=device_id).first()
        if not device:
            device = Device.objects.filter(imei=imei).first()

        if device:
            device.provider_device_id = device_reg_num or device.provider_device_id
            device.lock_provider = LockProvider.APPLE_MDM
            device.os_type = OSType.IOS
            device.serial_number = serial_number
            if imei2:
                device.secondary_imei = imei2
            device.save(update_fields=['provider_device_id', 'lock_provider', 'os_type', 'serial_number', 'secondary_imei'])

        return Response(res, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get', 'post'], url_path='apple-mdm/devices')
    def apple_mdm_devices(self, request):
        """
        GET/POST /api/devices/apple-mdm/devices/
        Fetches all registered Apple MDM devices and dashboard stats directly from Loksiz.
        """
        denied = self._block_customer(request)
        if denied:
            return denied

        page = request.data.get('page') or request.query_params.get('page', 1)
        limit = request.data.get('limit') or request.query_params.get('limit', 10)
        filter_status = request.data.get('filter') or request.query_params.get('filter')
        search = request.data.get('search') or request.query_params.get('search')

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.get_devices(
            page=page,
            limit=limit,
            filter_status=filter_status,
            search=search
        )
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='apple-action')
    def apple_action(self, request, pk=None):
        """
        POST /api/devices/{id}/apple-action/
        Executes custom LOCK or UNLOCK on an Apple MDM device.
        Parameters: action (LOCK/UNLOCK), message, phone_number, foot_note
        """
        denied = self._block_customer(request)
        if denied:
            return denied

        device = self.get_object()
        action_name = request.data.get('action', 'LOCK').upper()
        message = request.data.get('message')
        phone_number = request.data.get('phone_number') or request.data.get('phoneNumber') or getattr(device.retailer, 'mobile_number', '')
        foot_note = request.data.get('foot_note') or request.data.get('footNote') or "Prime Locker MDM Protection"

        device_reg = device.provider_device_id or device.serial_number or device.imei

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.take_action(
            device_reg_num=device_reg,
            action=action_name,
            message=message,
            phone_number=phone_number,
            foot_note=foot_note
        )

        if res.get('error') == 0:
            if action_name == 'LOCK':
                device.status = DeviceLockStatus.LOCKED
            elif action_name == 'UNLOCK':
                device.status = DeviceLockStatus.ACTIVE
            device.save(update_fields=['status'])

        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='apple-app-lock')
    def apple_app_lock(self, request, pk=None):
        """POST /api/devices/{id}/apple-app-lock/ - Enforces Single-App / Kiosk Mode."""
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device_reg = device.provider_device_id or device.serial_number or device.imei

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.application_lock(device_reg_num=device_reg)
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='apple-app-unlock')
    def apple_app_unlock(self, request, pk=None):
        """POST /api/devices/{id}/apple-app-unlock/ - Removes Single-App / Kiosk Mode restriction."""
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device_reg = device.provider_device_id or device.serial_number or device.imei

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.application_unlock(device_reg_num=device_reg)
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='apple-activation-lock')
    def apple_activation_lock(self, request, pk=None):
        """
        POST /api/devices/{id}/apple-activation-lock/
        action: 1 = Enable Supervised Activation Lock, 0 = Disable
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device_reg = device.provider_device_id or device.serial_number or device.imei
        act = request.data.get('action', 1)

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.activation_lock(device_reg_num=device_reg, action=act)
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'], url_path='apple-activation-lock-status')
    def apple_activation_lock_status(self, request, pk=None):
        """GET /api/devices/{id}/apple-activation-lock-status/ - Queries Supervised Activation Lock status."""
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device_reg = device.provider_device_id or device.serial_number or device.imei

        from .services.apple_mdm import AppleMDMService
        res = AppleMDMService.activation_lock_status(device_reg_num=device_reg)
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='apple-wallpaper')
    def apple_wallpaper(self, request, pk=None):
        """
        POST /api/devices/{id}/apple-wallpaper/
        Sets (Base64) or removes wallpaper on Lock Screen and Home Screen.
        Parameters: 'wallpaper_image' (base64 string) or 'action': 'REMOVE'
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device_reg = device.provider_device_id or device.serial_number or device.imei
        from .services.apple_mdm import AppleMDMService

        action_cmd = request.data.get('action', '').upper()
        if action_cmd == 'REMOVE':
            res = AppleMDMService.remove_wallpaper(device_reg_num=device_reg)
            device.wallpaper_locked = False
            device.save(update_fields=['wallpaper_locked'])
            return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

        wallpaper_b64 = request.data.get('wallpaper_image') or request.data.get('wallpaperImage')
        if not wallpaper_b64:
            return Response(
                {"error": 1, "msg": "Either 'wallpaper_image' (base64) or 'action': 'REMOVE' is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        res = AppleMDMService.set_wallpaper(device_reg_num=device_reg, wallpaper_image_base64=wallpaper_b64)
        if res.get('error') == 0:
            device.wallpaper_locked = True
            device.save(update_fields=['wallpaper_locked'])
        return Response(res, status=status.HTTP_200_OK if res.get('error') == 0 else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='apple-sync-customer')
    def apple_sync_customer(self, request, pk=None):
        """
        POST /api/devices/{id}/apple-sync-customer/
        Syncs local customer records and EMI contract details to Loksiz Apple MDM server.
        If device is not yet registered on Loksiz (e.g. was pending Apple Configurator),
        it calls add_device. If already registered, it calls update_device and add_emi_details.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()

        from .services.apple_mdm import AppleMDMService
        from emi.models import CustomerEMIContract

        customer = device.customer
        cust_name = getattr(customer, 'get_full_name', lambda: '')() or (customer.first_name if customer else '') or (customer.username if customer else '')
        cust_phone = getattr(customer, 'mobile_number', '') if customer else ''
        cust_email = getattr(customer, 'email', '') if customer else ''

        clean_imei = (device.imei or '').strip()
        clean_sn = (device.serial_number or '').strip()

        if not clean_imei and not clean_sn:
            return Response(
                {"error": 1, "msg": "Device must have an IMEI or Serial Number to sync with Apple MDM."},
                status=status.HTTP_400_BAD_REQUEST
            )

        sync_results = {}

        # 1. Register or Update Device & Customer Info on Loksiz
        if device.provider_device_id:
            update_res = AppleMDMService.update_device(
                device_reg_num=device.provider_device_id,
                customer_name=cust_name,
                customer_mobile=cust_phone,
                customer_email=cust_email,
                imei=clean_imei,
                serial_number=clean_sn
            )
            sync_results['device_sync'] = update_res
            if update_res.get('error') != 0:
                add_res = AppleMDMService.add_device(
                    imei=clean_imei,
                    serial_number=clean_sn or clean_imei,
                    customer_name=cust_name,
                    customer_mobile=cust_phone,
                    customer_email=cust_email,
                    imei2=device.secondary_imei
                )
                sync_results['device_sync'] = add_res
                if add_res.get('error') == 0:
                    device_reg = (add_res.get('data') or {}).get('deviceRegNum') or ''
                    if device_reg:
                        device.provider_device_id = str(device_reg)
                        device.lock_provider = LockProvider.APPLE_MDM
                        device.os_type = OSType.IOS
                        device.save(update_fields=['provider_device_id', 'lock_provider', 'os_type'])
        else:
            add_res = AppleMDMService.add_device(
                imei=clean_imei,
                serial_number=clean_sn or clean_imei,
                customer_name=cust_name,
                customer_mobile=cust_phone,
                customer_email=cust_email,
                imei2=device.secondary_imei
            )
            sync_results['device_sync'] = add_res
            if add_res.get('error') == 0:
                device_reg = (add_res.get('data') or {}).get('deviceRegNum') or ''
                if device_reg:
                    device.provider_device_id = str(device_reg)
                    device.lock_provider = LockProvider.APPLE_MDM
                    device.os_type = OSType.IOS
                    device.save(update_fields=['provider_device_id', 'lock_provider', 'os_type'])

        # 2. Sync EMI details if contract exists
        contract = getattr(device, 'emi_contract', None) or CustomerEMIContract.objects.filter(device_imei=device.imei).first()
        dev_reg = device.provider_device_id
        if contract and dev_reg:
            emi_res = AppleMDMService.add_emi_details(
                device_reg_num=dev_reg,
                loan_no=contract.loan_account_number or f"LOAN-{contract.id}",
                finance_type_bank=contract.loan_provider or "Prime Locker",
                product_price=float(contract.product_price or contract.financed_amount),
                down_payment=float(contract.down_payment or 0),
                balance_amount=float(contract.financed_amount),
                tenure=int(contract.tenure_months),
                per_month_emi=float(contract.monthly_emi_amount),
                due_date=str(contract.first_emi_date),
                tenure_type="MONTHLY"
            )
            sync_results['emi_sync'] = emi_res

        has_error = sync_results.get('device_sync', {}).get('error', 0) != 0
        return Response({
            "message": "Apple MDM sync executed successfully." if not has_error else "Apple MDM sync encountered errors.",
            "device_id": device.id,
            "provider_device_id": device.provider_device_id,
            "results": sync_results
        }, status=status.HTTP_400_BAD_REQUEST if has_error else status.HTTP_200_OK)



class DeviceHeartbeatView(APIView):
    """
    Silent Heartbeat Ping API for the Android Locker APK or TV App.
    Pings periodically to update online status, report GPS, and fetch lock status / policies.
    Supports both POST and GET query methods.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        data = request.query_params.dict()
        return self._process_heartbeat(request, data)

    def post(self, request):
        return self._process_heartbeat(request, request.data)

    def _process_heartbeat(self, request, raw_data):
        serializer = DeviceHeartbeatSerializer(data=raw_data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        imei = serializer.validated_data['imei']
        device = Device.objects.filter(imei=imei).first()
        if not device:
            return Response({"error": "Device not registered in system."}, status=status.HTTP_404_NOT_FOUND)

        # Update telemetry
        device.last_online_at = timezone.now()
        device.ip_address = get_client_ip(request)
        if not getattr(device, 'is_activated', False):
            device.is_activated = True
            device.activated_at = timezone.now()
            if device.status == DeviceLockStatus.INACTIVE:
                device.status = DeviceLockStatus.ACTIVE

        if 'battery_level' in serializer.validated_data:
            device.battery_level = serializer.validated_data['battery_level']
        if 'app_version' in serializer.validated_data:
            device.app_version = serializer.validated_data['app_version']
        if 'fcm_token' in serializer.validated_data and serializer.validated_data['fcm_token']:
            device.fcm_token = serializer.validated_data['fcm_token']

        # Location recording
        lat = serializer.validated_data.get('latitude')
        lng = serializer.validated_data.get('longitude')
        addr = serializer.validated_data.get('address')
        if lat and lng:
            device.current_latitude = lat
            device.current_longitude = lng
            device.current_address = addr
            device.last_location_at = timezone.now()
            DeviceLocationLog.objects.create(
                device=device,
                latitude=lat,
                longitude=lng,
                address=addr
            )

        if 'synced_contacts' in serializer.validated_data:
            device.synced_contacts = serializer.validated_data['synced_contacts']

        # Check for SIM swap / change
        incoming_serial = serializer.validated_data.get('sim_serial')
        incoming_operator = serializer.validated_data.get('sim_operator')
        incoming_number = serializer.validated_data.get('sim_number')

        sim_swap_detected = False
        if device.sim_serial and incoming_serial and (device.sim_serial.strip() != incoming_serial.strip()):
            sim_swap_detected = True
        elif not device.sim_serial and device.sim_number and incoming_number and (device.sim_number.strip() != incoming_number.strip()):
            sim_swap_detected = True

        if sim_swap_detected:
            device.sim_changed = True
            device.sim_change_detected_at = timezone.now()
            if not isinstance(device.sim_history, list):
                device.sim_history = []
            device.sim_history.append({
                "timestamp": timezone.now().isoformat(),
                "previous_sim": {
                    "operator": device.sim_operator,
                    "number": device.sim_number,
                    "serial": device.sim_serial
                },
                "new_sim": {
                    "operator": incoming_operator or device.sim_operator,
                    "number": incoming_number or device.sim_number,
                    "serial": incoming_serial or device.sim_serial
                },
                "reason": "SIM swap detected via device telemetry"
            })
            AuditLog.objects.create(
                user=device.customer or device.retailer,
                action="DEVICE_SIM_CHANGED",
                ip_address=get_client_ip(request),
                details={
                    "device_id": device.id,
                    "imei": device.imei,
                    "previous_sim_operator": device.sim_operator,
                    "previous_sim_serial": device.sim_serial,
                    "new_sim_operator": incoming_operator,
                    "new_sim_serial": incoming_serial
                }
            )
            if device.retailer:
                try:
                    from accounts.views import send_system_notification
                    send_system_notification(
                        recipient=device.retailer,
                        title="⚠️ SIM Change Detected",
                        message=f"Device {device.device_name or device.imei} has changed SIM to {incoming_operator or 'New SIM'}.",
                        notification_type='SYSTEM',
                        reference_id=device.id
                    )
                except Exception:
                    pass

        for sim_f in ['sim_operator', 'sim_number', 'sim_serial', 'sim_operator_2', 'sim_number_2']:
            if sim_f in serializer.validated_data and serializer.validated_data[sim_f]:
                setattr(device, sim_f, serializer.validated_data[sim_f])

        # Update real hardware model if sent by device APK and currently has TAC/generic guess
        reported_model = serializer.validated_data.get('model') or serializer.validated_data.get('device_model')
        reported_brand = serializer.validated_data.get('brand')
        reported_name = serializer.validated_data.get('device_name')
        if reported_model:
            is_generic = (
                not device.model or
                str(device.model).startswith('Model TAC-') or
                str(device.model).startswith('Model 35') or
                device.model in ('Unknown', 'Android Device')
            )
            if is_generic:
                device.model = str(reported_model).strip()
                if reported_brand:
                    device.brand = str(reported_brand).strip()
                device.device_name = str(reported_name).strip() if reported_name else f"{device.brand} {device.model}".strip()

        device.save()

        # Fetch pending commands
        pending_commands = device.commands.filter(status=CommandStatus.PENDING)
        commands_data = [
            {"id": c.id, "command_type": c.command_type, "parameters": c.parameters}
            for c in pending_commands
        ]
        # Mark fetched commands as executed
        pending_commands.update(status=CommandStatus.EXECUTED, executed_at=timezone.now())

        if not device.offline_unlock_code:
            from .models import generate_offline_code
            device.offline_unlock_code = generate_offline_code()
            device.save(update_fields=['offline_unlock_code'])

        from accounts.models import PlatformSettings
        ps = PlatformSettings.get_settings()

        retailer = device.retailer
        r_name = ""
        r_phone = ""
        r_business = ""
        if retailer:
            r_name = (retailer.business_name or retailer.get_full_name() or retailer.username).strip()
            r_phone = str(retailer.store_phone or retailer.mobile_number or "").strip()
            r_business = str(retailer.business_name or r_name).strip()

        customer = device.customer
        c_name = ""
        c_phone = ""
        if customer:
            c_name = (customer.get_full_name() or customer.username).strip()
            c_phone = str(customer.mobile_number or "").strip()

        if not r_name:
            r_name = ps.company_name or "Prime Locker"
            r_business = ps.company_name or "Prime Locker"
        if not r_phone:
            r_phone = ps.support_phone or ""

        return Response({
            "imei": device.imei,
            "status": device.status,
            "lock_message": device.lock_message,
            "retailer_name": r_name,
            "retailer_phone": r_phone,
            "retailer_mobile": r_phone,
            "store_name": r_business or r_name,
            "store_phone": r_phone,
            "retailer_details": {
                "id": retailer.id if retailer else None,
                "name": r_name,
                "business_name": r_business,
                "mobile_number": r_phone,
                "phone": r_phone,
                "store_phone": r_phone
            },
            "customer_name": c_name,
            "customer_phone": c_phone,
            "customer_mobile": c_phone,
            "customer_details": {
                "id": customer.id if customer else None,
                "name": c_name,
                "mobile_number": c_phone,
                "phone": c_phone
            },
            "support_phone": ps.support_phone,
            "support_phones": ps.support_phones,
            "support_phones_list": ps.get_support_phones_list(),
            "offline_unlock_code": device.offline_unlock_code,
            "code": device.offline_unlock_code,
            "unlock_code": device.offline_unlock_code,
            "offline_code_expires_at": device.offline_code_expires_at,
            "master_recovery_code": get_master_offline_code(device.imei),
            "policies": {
                "block_factory_reset": device.block_factory_reset,
                "frp_protection_enabled": device.frp_protection_enabled,
                "camera_disabled": device.camera_disabled,
                "social_media_disabled": device.social_media_disabled,
                "whatsapp_disabled": device.whatsapp_disabled,
                "instagram_disabled": device.instagram_disabled,
                "facebook_disabled": device.facebook_disabled,
                "wallpaper_locked": device.wallpaper_locked,
                "custom_wallpaper_url": device.custom_wallpaper_url or (device.custom_wallpaper.url if device.custom_wallpaper else None),
            },
            "commands": commands_data
        })


class AppVersionViewSet(viewsets.ModelViewSet):
    """
    Module 2: APK and App Versions Management (Sidebar -> App Versions).
    Super Admin can upload new APKs and mark releases.
    Retailers/Users can download the latest APKs or get the QR provisioning link.
    """
    permission_classes = [IsSuperAdminOrReadOnly]
    queryset = AppVersion.objects.all()
    serializer_class = AppVersionSerializer

    def perform_create(self, serializer):
        serializer.save(uploaded_by=self.request.user)

    @action(detail=False, methods=['get'])
    def latest(self, request):
        """Returns the latest active version for the requested app_type"""
        app_type = request.query_params.get('app_type', AppType.ANDROID_LOCKER)
        latest_version = AppVersion.objects.filter(app_type=app_type, is_active=True).first()
        if not latest_version:
            return Response({"detail": f"No active version found for {app_type}."}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(latest_version)
        data = serializer.data
        # Generate QR code URL helper
        qr_target = data.get('download_url') or data.get('external_download_url') or ""
        data['qr_code_generator_url'] = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={qr_target}" if qr_target else None
        return Response(data)


class LockMessageTemplateViewSet(viewsets.ModelViewSet):
    """
    Screen A-40 Lock Message Templates (Missing API M2).
    Pre-configured messages displayed on locked phone/TV screens.
    GET /api/lock-messages/ : list all templates
    POST /api/lock-messages/ : create template (Super Admin, Staff Operations)
    PATCH/DELETE /api/lock-messages/:id/ : update / remove template
    """
    from .models import LockMessageTemplate
    from .serializers import LockMessageTemplateSerializer
    queryset = LockMessageTemplate.objects.all()
    serializer_class = LockMessageTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can create lock message templates.")
        if serializer.validated_data.get('is_default'):
            from .models import LockMessageTemplate
            LockMessageTemplate.objects.filter(is_default=True).update(is_default=False)
        serializer.save(created_by=user)

    def perform_update(self, serializer):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can edit lock message templates.")
        if serializer.validated_data.get('is_default'):
            from .models import LockMessageTemplate
            LockMessageTemplate.objects.filter(is_default=True).update(is_default=False)
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can delete lock message templates.")
        instance.delete()


class RocketPayWebhookView(APIView):
    """
    RocketPay OEM DLC Webhook Callback Handler (Section B Item 6).
    Receives real-time device lifecycle callbacks:
    Events: 'enrolled', 'locked', 'unlocked', 'failed', 'tampered'
    URL: /api/integrations/rocketpay/webhook/
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from .services.rocketpay_service import RocketPayService
        signature_header = (
            request.headers.get('X-RocketPay-Signature') or
            request.headers.get('X-Signature') or
            request.headers.get('Signature') or
            ''
        )
        is_valid = RocketPayService.verify_webhook_signature(request.body, signature_header)
        if not is_valid:
            return Response(
                {"error": "Invalid or missing webhook signature"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        result = RocketPayService.process_webhook_payload(request.data)
        return Response(result, status=status.HTTP_200_OK)


class AppleMDMWebhookView(APIView):
    """
    Apple MDM (Loksiz v1.4) Real-Time Webhook Callback Handler.
    Receives 'apple_device_status_update' events for:
      - activation_status (MDM profile enrollment / uninstallation)
      - lock_status (Remote lock / unlock)
      - app_lock_status (Kiosk single-app lock / unlock)
      - wallpaper_status (Wallpaper applied / removed)
      - device_location (Live GPS coordinates: lat, long, date)
    URL: /api/integrations/apple-mdm/webhook/
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from .services.apple_mdm import AppleMDMService
        result = AppleMDMService.process_webhook_payload(request.data, headers=request.headers)
        return Response(result, status=status.HTTP_200_OK)


class ApprovedDeviceTACViewSet(viewsets.ModelViewSet):
    """
    Approved phone models (TAC List) for OEM / DLC locking (Item 12).
    GET /api/tac-list/ : list all approved TAC entries
    POST /api/tac-list/ : add new approved TAC
    PATCH /api/tac-list/:id/ : update TAC entry
    DELETE /api/tac-list/:id/ : remove TAC entry
    GET /api/tac-list/check/?imei=... : checks if device IMEI is approved
    """
    from .models import ApprovedDeviceTAC
    from .serializers import ApprovedDeviceTACSerializer
    queryset = ApprovedDeviceTAC.objects.all()
    serializer_class = ApprovedDeviceTACSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        from .models import ApprovedDeviceTAC
        qs = ApprovedDeviceTAC.objects.all()
        env = self.request.query_params.get('environment')
        if env:
            qs = qs.filter(models.Q(environment=env.upper()) | models.Q(environment='ALL'))
        brand = self.request.query_params.get('brand')
        if brand:
            qs = qs.filter(brand__icontains=brand)
        active = self.request.query_params.get('active')
        if active is not None:
            qs = qs.filter(active=active.lower() == 'true')
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can manage approved TAC list.")
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can manage approved TAC list.")
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can manage approved TAC list.")
        instance.delete()

    @action(detail=False, methods=['get'])
    def check(self, request):
        """
        GET /api/tac-list/check/?imei=...
        Checks if device IMEI (first 8 digits) is on the approved TAC list (Item 12 & RP12).
        First queries RocketPay's live device catalogue; falls back to local TAC whitelist.
        """
        imei = request.query_params.get('imei', '').strip()
        if len(imei) < 8:
            return Response(
                {"eligible": False, "error": "Valid IMEI with at least 8 digits required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        tac = imei[:8]
        from accounts.models import PlatformSettings
        ps = PlatformSettings.get_settings()
        current_env = 'STAGING' if ('staging' in (ps.rocketpay_base_url or '').lower()) else 'PRODUCTION'

        # RP12: Check RocketPay's live device catalogue first
        try:
            rp_cat = RocketPayService.check_device_catalogue(imei)
            if rp_cat.get('eligible'):
                return Response({
                    "eligible": True,
                    "tac": tac,
                    "brand": rp_cat.get('brand'),
                    "model_number": rp_cat.get('model'),
                    "marketing_name": rp_cat.get('marketing_name'),
                    "mdm_type": rp_cat.get('mdm_type', 'DLC'),
                    "environment": current_env,
                    "source": "ROCKETPAY_CATALOGUE",
                    "message": f"Device model {rp_cat.get('brand')} {rp_cat.get('marketing_name') or rp_cat.get('model')} is approved by RocketPay."
                })
        except Exception:
            pass

        from .models import ApprovedDeviceTAC
        tac_entry = ApprovedDeviceTAC.objects.filter(
            tac=tac,
            environment__in=[current_env, 'ALL'],
            active=True
        ).first()

        if tac_entry:
            return Response({
                "eligible": True,
                "tac": tac,
                "brand": tac_entry.brand,
                "model_number": tac_entry.model_number,
                "marketing_name": tac_entry.marketing_name,
                "environment": tac_entry.environment,
                "source": "LOCAL_TAC_WHITELIST",
                "message": f"Device model {tac_entry.brand} {tac_entry.marketing_name or tac_entry.model_number} is approved for DLC enrollment."
            })
        else:
            return Response({
                "eligible": False,
                "tac": tac,
                "environment": current_env,
                "message": f"Device with TAC {tac} is not approved for {current_env} enrollment."
            }, status=status.HTTP_200_OK)





class InstallationVideoViewSet(viewsets.ModelViewSet):
    """
    Installation & Training Videos API.
    - Super-Admin: Full management (Create, Update, Delete, Reorder, Upload video/thumbnail).
    - Retailers / Distributors: Read-only access to active videos for their role or ALL.
    Supports filtering by category: ?category=ANDROID_DPC or ?category=DLC_LOCKER or ?category=IPHONE
    """
    permission_classes = [IsSuperAdminOrReadOnly]
    serializer_class = InstallationVideoSerializer
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        # Super-Admin sees all videos (active and inactive)
        if getattr(user, 'role', '') == 'SUPER_ADMIN':
            qs = InstallationVideo.objects.all()
        else:
            # Retailers, Distributors, etc. see active videos matching their role or ALL
            qs = InstallationVideo.objects.filter(is_active=True)
            if hasattr(user, 'role') and user.role:
                qs = qs.filter(Q(target_role='ALL') | Q(target_role=user.role))

        # Filter by category if requested (e.g. ANDROID_DPC, DLC_LOCKER, IPHONE)
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category.upper())

        return qs.order_by('display_order', '-created_at')

    def perform_create(self, serializer):
        instance = serializer.save(created_by=self.request.user)
        AuditLog.objects.create(
            user=self.request.user,
            action="CREATED_INSTALLATION_VIDEO",
            ip_address=get_client_ip(self.request),
            details={
                "video_id": instance.id,
                "title": instance.title,
                "category": instance.category,
                "source_type": instance.source_type
            }
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            user=self.request.user,
            action="UPDATED_INSTALLATION_VIDEO",
            ip_address=get_client_ip(self.request),
            details={"video_id": instance.id, "title": instance.title}
        )

    def perform_destroy(self, instance):
        AuditLog.objects.create(
            user=self.request.user,
            action="DELETED_INSTALLATION_VIDEO",
            ip_address=get_client_ip(self.request),
            details={"video_id": instance.id, "title": instance.title}
        )
        instance.delete()

    @action(detail=False, methods=['get'], url_path='enrollment-options')
    def enrollment_options(self, request):
        """
        Return the three Installation Video enrollment options in fixed display order:
        New Device Enrollment, Running Device Enrollment, WiFi Activation Enrollment.
        """
        options_spec = [
            (VideoCategory.NEW_DEVICE, VideoCategory.NEW_DEVICE.label),
            (VideoCategory.RUNNING_DEVICE, VideoCategory.RUNNING_DEVICE.label),
            (VideoCategory.WIFI_ACTIVATION, VideoCategory.WIFI_ACTIVATION.label),
        ]
        qs = self.get_queryset().filter(
            is_active=True,
            category__in=[cat for cat, _ in options_spec],
        ).order_by('display_order', '-created_at')

        videos_by_category = {}
        for video in qs:
            videos_by_category.setdefault(video.category, video)

        options = []
        for cat, label in options_spec:
            video = videos_by_category.get(cat)
            serialized = self.get_serializer(video).data if video else None
            options.append({
                'category': cat,
                'label': label,
                'title': video.title if video else label,
                'youtube_url': serialized['stream_url'] if serialized else None,
                'video_url': serialized['video_url'] if serialized else None,
                'video': serialized,
            })

        return Response({'options': options})

PUBLIC_APP_SLUGS = {
    'customer': AppType.CUSTOMER_APP,
    'customer-app': AppType.CUSTOMER_APP,
    'retailer': AppType.RETAILER_APP,
    'retailer-app': AppType.RETAILER_APP,
    'android-locker': AppType.ANDROID_LOCKER,
    'locker': AppType.ANDROID_LOCKER,
    'tv': AppType.TV_LOCKER,
    'tv-locker': AppType.TV_LOCKER,
}


class PublicAppDownloadView(APIView):
    """
    Public, no-login direct APK download route.
    GET /api/app-versions/download/<slug>/ (e.g. /api/app-versions/download/customer/)
    Streams the latest live APK binary directly, enabling public QR-code scanning
    without requiring authentication or 401 errors.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        import os
        from django.http import FileResponse, HttpResponseRedirect

        clean_slug = str(slug).strip().lower()
        app_type = PUBLIC_APP_SLUGS.get(clean_slug)
        if not app_type:
            # Fallback: check raw AppType choices
            for at in AppType:
                if at.value.lower() == clean_slug or at.name.lower() == clean_slug:
                    app_type = at.value
                    break

        if not app_type:
            return Response(
                {
                    "detail": f"Unknown app download slug '{slug}'.",
                    "available_slugs": list(PUBLIC_APP_SLUGS.keys())
                },
                status=status.HTTP_404_NOT_FOUND
            )

        latest_version = AppVersion.objects.filter(
            app_type=app_type,
            is_active=True
        ).order_by('-version_code', '-created_at').first()

        if not latest_version:
            return Response(
                {"detail": f"No active APK release found for '{slug}' ({app_type}). Please upload a version in Super Admin portal."},
                status=status.HTTP_404_NOT_FOUND
            )

        # Redirect to external CDN URL if configured
        if latest_version.external_download_url:
            return HttpResponseRedirect(latest_version.external_download_url)

        if not latest_version.apk_file or not os.path.exists(latest_version.apk_file.path):
            return Response(
                {"detail": f"APK binary file for version {latest_version.version_name} is missing from the server storage."},
                status=status.HTTP_404_NOT_FOUND
            )

        filename = os.path.basename(latest_version.apk_file.name)
        response = FileResponse(
            latest_version.apk_file.open('rb'),
            content_type='application/vnd.android.package-archive',
            as_attachment=True,
            filename=filename
        )
        return response


class RetailerEnrollmentQRsView(APIView):
    """
    GET /mdm/enrollment-qrs/ (or /api/mdm/enrollment-qrs/)
    Returns generic, retailer-level MDM provisioning QRs for setting up phones:
      - qr_service_app (New device / 7-tap)
      - qr_running_device (Running device - normal)
      - qr_wireless (Running device - wireless, null if unsupported)
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from .services.partner_mdm_service import PartnerMDMService
        qr_data = PartnerMDMService.get_generic_enrollment_qrs(user=request.user)
        retailer_name = getattr(request.user, 'business_name', '') or getattr(request.user, 'username', '') if getattr(request.user, 'is_authenticated', False) else "Prime Retailer"
        retailer_id = request.user.id if getattr(request.user, 'is_authenticated', False) else None
        apk_download_url = "https://apirenewjss.com/apk/primelocker.apk"
        camera_download_qr = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={apk_download_url}"
        service_app_qr = qr_data.get("qr_service_app")
        running_device_qr = qr_data.get("qr_running_device")
        test_dpc_qr = qr_data.get("qr_test_dpc")
        wireless_qr = qr_data.get("qr_wireless")
        return Response({
            "status": "success",
            "retailer": retailer_name,
            "retailer_id": retailer_id,
            "step1_connect_app": camera_download_qr,
            "step2_install_app": running_device_qr,
            "camera_download_qr": camera_download_qr,
            "qr_service_app": service_app_qr,
            "qr_running_device": running_device_qr,
            "qr_wireless": wireless_qr,
            "qr_test_dpc": test_dpc_qr,
            "app_download_url": apk_download_url,
            "provider": qr_data.get("provider", "PARTNER_MDM"),
            "simulation": qr_data.get("simulation", False),
            "instructions": {
                "step1": "Scan with standard camera or Prime app to download & connect Prime Locker APK",
                "step2": "Scan to configure MDM running profile / permissions on device",
                "new_device_6tap": "Use qr_test_dpc strictly on factory reset welcome screen (tap 6 times on blank area)"
            },
            "data": qr_data
        }, status=status.HTTP_200_OK)


class VerifyOfflineCodeView(APIView):
    """
    Direct endpoint: POST /api/devices/verify-offline-code/
    Validates offline unlock code by IMEI or device_id for mobile lock screens, retailer scanners, and diagnostics.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        imei = str(request.data.get('imei') or "").strip()
        device_id = request.data.get('device_id')
        input_code = str(
            request.data.get('code') or
            request.data.get('offline_unlock_code') or
            request.data.get('unlock_code') or
            ""
        ).strip()

        if not input_code:
            return Response(
                {"success": False, "valid": False, "error": "Verification code is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        device = None
        if device_id:
            device = Device.objects.filter(id=device_id).first()
        elif imei:
            device = Device.objects.filter(imei=imei).first()

        if not device:
            return Response(
                {"success": False, "valid": False, "error": "Device not found for provided IMEI or ID."},
                status=status.HTTP_404_NOT_FOUND
            )

        master_code = get_master_offline_code(device.imei)
        is_code_match = (input_code == str(device.offline_unlock_code).strip())
        is_master_match = bool(master_code and input_code == master_code)

        if not (is_code_match or is_master_match):
            return Response({
                "success": False,
                "valid": False,
                "device_id": device.id,
                "imei": device.imei,
                "error": "Invalid offline unlock code."
            }, status=status.HTTP_400_BAD_REQUEST)

        if is_code_match and not is_master_match:
            if device.offline_code_expires_at and timezone.now() > device.offline_code_expires_at:
                return Response({
                    "success": False,
                    "valid": False,
                    "device_id": device.id,
                    "imei": device.imei,
                    "error": "Offline unlock code has expired. Please request a new code.",
                    "is_expired": True
                }, status=status.HTTP_400_BAD_REQUEST)

        should_unlock = request.data.get('unlock_device') or request.data.get('auto_unlock')
        if isinstance(should_unlock, str):
            should_unlock = should_unlock.lower() in ('true', '1', 'yes')

        if should_unlock and device.status == DeviceLockStatus.LOCKED:
            device.status = DeviceLockStatus.ACTIVE
            device.save(update_fields=['status'])
            DeviceCommand.objects.create(
                device=device,
                command_type=CommandType.UNLOCK,
                parameters={}
            )
            if device.emi_contract:
                device.emi_contract.status = EMIContractStatus.ACTIVE
                device.emi_contract.save(update_fields=['status'])

            AuditLog.objects.create(
                user=getattr(request, 'user', None) if getattr(request, 'user', None) and request.user.is_authenticated else (device.retailer or device.customer),
                action="OFFLINE_CODE_UNLOCKED",
                ip_address=get_client_ip(request),
                details={
                    "device_id": device.id,
                    "imei": device.imei,
                    "unlocked_via": "MASTER_CODE" if is_master_match else "OFFLINE_PIN"
                }
            )

        return Response({
            "success": True,
            "valid": True,
            "device_id": device.id,
            "imei": device.imei,
            "device_status": device.status,
            "is_master_code": is_master_match,
            "message": "Offline unlock code is valid." + (" Device has been unlocked." if should_unlock else "")
        })

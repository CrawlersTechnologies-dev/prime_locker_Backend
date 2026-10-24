from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db import transaction
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
    AppType
)
from .serializers import (
    DeviceSerializer,
    DeviceCommandSerializer,
    DeviceLocationLogSerializer,
    AppVersionSerializer,
    DeviceHeartbeatSerializer
)
from .imei_lookup import lookup_imei
from accounts.models import User, AuditLog
from accounts.views import get_client_ip, get_all_downstream_users
from decimal import Decimal
from emi.models import CustomerEMIContract, EMIContractStatus, EMIPlanTemplate, EMIInstallment, InstallmentStatus, add_months


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

    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        self._create_command(device, CommandType.REBOOT)
        return Response({"message": f"Remote reboot command queued for device {device.imei}."})

    @action(detail=True, methods=['post'], url_path='toggle-camera')
    def toggle_camera(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device.camera_disabled = not device.camera_disabled
        device.save(update_fields=['camera_disabled'])
        self._create_command(device, CommandType.UPDATE_POLICY, {"camera_disabled": device.camera_disabled})
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
        device.social_media_disabled = not device.social_media_disabled
        device.whatsapp_disabled = device.social_media_disabled
        device.instagram_disabled = device.social_media_disabled
        device.facebook_disabled = device.social_media_disabled
        device.save(update_fields=['social_media_disabled', 'whatsapp_disabled', 'instagram_disabled', 'facebook_disabled'])
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

    @action(detail=True, methods=['post'], url_path='generate-offline-code')
    def generate_offline_code_view(self, request, pk=None):
        """
        Generates a secure offline unlock code with expiry options (Missing API M7).
        expiry_option choices: 'UNTIL_NEXT_DUE_DATE' (default), '24_HOURS', 'PERMANENT'
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        from .models import generate_offline_code
        device = self.get_object()
        new_code = generate_offline_code()

        expiry_option = request.data.get('expiry_option', 'UNTIL_NEXT_DUE_DATE').upper()
        expires_at = None

        if expiry_option == '24_HOURS':
            expires_at = timezone.now() + timedelta(hours=24)
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
                "expires_at": str(expires_at)
            }
        )

        return Response({
            "message": "New offline unlock code generated.",
            "device_imei": device.imei,
            "offline_unlock_code": new_code,
            "expiry_option": expiry_option,
            "expires_at": expires_at
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
        Per-app lock (Missing API M10): locks specified package name(s).
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        package_name = request.data.get('package_name')
        package_names = request.data.get('package_names', [])
        if package_name and package_name not in package_names:
            package_names.append(package_name)

        if not package_names:
            return Response({"detail": "Field 'package_name' or 'package_names' is required."}, status=status.HTTP_400_BAD_REQUEST)

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
        Per-app unlock (Missing API M10): removes package name from locked list.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        package_name = request.data.get('package_name')
        if not package_name:
            return Response({"detail": "Field 'package_name' is required."}, status=status.HTTP_400_BAD_REQUEST)

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
        Audio Reminder (Missing API M10): plays sound/voice reminder on the phone/TV.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        message = request.data.get('message', 'Please pay your EMI to avoid device locking.')
        volume = int(request.data.get('volume', 100))

        self._create_command(device, CommandType.PUSH_MESSAGE, {
            "action": "AUDIO_REMINDER",
            "message": message,
            "volume": volume
        })
        return Response({"message": f"Audio reminder command dispatched to device {device.imei}."})

    @action(detail=True, methods=['post'], url_path='block-streaming')
    def block_streaming(self, request, pk=None):
        """
        TV/Mobile Streaming app block (Missing API M10): blocks Netflix, Prime Video, Hotstar, YouTube.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        streaming_blocked = request.data.get('streaming_blocked')
        if streaming_blocked is None:
            streaming_blocked = not device.streaming_blocked

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
        Refresh Location Now (Missing API M10): immediately requests live GPS sync from device agent.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
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

    @action(detail=True, methods=['post'], url_path='toggle-reset-block')
    def toggle_reset_block(self, request, pk=None):
        """
        Reset Option Block & FRP Protection:
        Remotely blocks/unblocks factory data reset and enables/disables FRP lock resistance.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
        device.block_factory_reset = not device.block_factory_reset
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

    @action(detail=True, methods=['post'], url_path='set-wallpaper')
    def set_wallpaper(self, request, pk=None):
        """
        Wallpaper Control:
        Changes, locks, or customizes the lock wallpaper shown on the locked device.
        """
        denied = self._block_customer(request)
        if denied:
            return denied
        device = self.get_object()
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


class DeviceHeartbeatView(APIView):
    """
    Silent Heartbeat Ping API for the Android Locker APK or TV App.
    Pings periodically to update online status, report GPS, and fetch lock status / policies.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = DeviceHeartbeatSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        imei = serializer.validated_data['imei']
        device = Device.objects.filter(imei=imei).first()
        if not device:
            return Response({"error": "Device not registered in system."}, status=status.HTTP_404_NOT_FOUND)

        # Update telemetry
        device.last_online_at = timezone.now()
        device.ip_address = get_client_ip(request)

        if 'battery_level' in serializer.validated_data:
            device.battery_level = serializer.validated_data['battery_level']
        if 'app_version' in serializer.validated_data:
            device.app_version = serializer.validated_data['app_version']

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

        device.save()

        # Fetch pending commands
        pending_commands = device.commands.filter(status=CommandStatus.PENDING)
        commands_data = [
            {"id": c.id, "command_type": c.command_type, "parameters": c.parameters}
            for c in pending_commands
        ]
        # Mark fetched commands as executed
        pending_commands.update(status=CommandStatus.EXECUTED, executed_at=timezone.now())

        return Response({
            "imei": device.imei,
            "status": device.status,
            "lock_message": device.lock_message,
            "offline_unlock_code": device.offline_unlock_code,
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
            return Response({"error": "Invalid webhook signature"}, status=status.HTTP_400_BAD_REQUEST)

        result = RocketPayService.process_webhook_payload(request.data)
        return Response(result, status=status.HTTP_200_OK)

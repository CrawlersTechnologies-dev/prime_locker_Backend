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
        if user.role == 'SUPER_ADMIN':
            qs = Device.objects.all()
        elif user.role == 'RETAILER':
            qs = Device.objects.filter(retailer=user)
        elif user.role == 'CUSTOMER':
            qs = Device.objects.filter(customer=user)
        else:
            downline_users = get_all_downstream_users(user)
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
        return DeviceCommand.objects.create(
            device=device,
            command_type=command_type,
            parameters=params or {},
            issued_by=self.request.user
        )

    def _block_customer(self, request):
        if request.user.role == 'CUSTOMER':
            return Response({"detail": "Customers do not have permission to control devices."}, status=status.HTTP_403_FORBIDDEN)
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

        AuditLog.objects.create(
            user=request.user,
            action="REMOTE_LOCK_DEVICE",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei}
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

        AuditLog.objects.create(
            user=request.user,
            action="REMOTE_UNLOCK_DEVICE",
            ip_address=get_client_ip(request),
            details={"device_id": device.id, "imei": device.imei}
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

    @action(detail=True, methods=['post'], url_path='generate-offline-code')
    def generate_offline_code_view(self, request, pk=None):
        denied = self._block_customer(request)
        if denied:
            return denied
        from .models import generate_offline_code
        device = self.get_object()
        new_code = generate_offline_code()
        device.offline_unlock_code = new_code
        device.save(update_fields=['offline_unlock_code'])
        return Response({
            "message": "New offline unlock code generated.",
            "device_imei": device.imei,
            "offline_unlock_code": new_code
        })

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

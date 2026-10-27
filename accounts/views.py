import os
import mimetypes
from django.http import FileResponse, Http404
from django.conf import settings
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from django.db import transaction, models
from django.contrib.auth import get_user_model
from .serializers import (
    CustomTokenObtainPairSerializer, 
    UserSerializer, 
    SuperDistributorSerializer,
    DistributorSerializer,
    SubDistributorSerializer,
    RetailerSerializer,
    CustomerSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    AuditLogSerializer
)
from .models import AuditLog, PasswordResetOTP
import random

User = get_user_model()

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0]
    return request.META.get('REMOTE_ADDR')

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            # Login successful, create audit log
            # The serializer valid method can be used to get the user, but we can also get it from username
            username = request.data.get('username')
            user = User.objects.filter(username=username).first() or \
                   User.objects.filter(email=username).first() or \
                   User.objects.filter(mobile_number=username).first()
            if user:
                AuditLog.objects.create(
                    user=user,
                    action="USER_LOGIN",
                    ip_address=get_client_ip(request),
                    details={"method": "password"}
                )
        return response

from .permissions import RoleHierarchyPermission

class BaseRoleViewSet(viewsets.ModelViewSet):
    """Base ViewSet to handle common logic for all role-based user management."""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, RoleHierarchyPermission]
    target_role = None  # To be overridden by subclasses
    ordering = ['-date_joined']

    def get_queryset(self):
        user = self.request.user
        queryset = User.objects.filter(role=self.target_role)
        
        from .models import is_company_user
        if not is_company_user(user):
            downline_ids = [u.id for u in user.get_all_subordinates() if u.role == self.target_role]
            queryset = queryset.filter(id__in=downline_ids)

        parent_param = self.request.query_params.get('parent')
        if parent_param:
            queryset = queryset.filter(parent_id=parent_param)

        search = self.request.query_params.get('search')
        if search:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(username__icontains=search) |
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search) |
                Q(email__icontains=search) |
                Q(mobile_number__icontains=search) |
                Q(business_name__icontains=search)
            )

        ordering_param = self.request.query_params.get('ordering')
        if ordering_param:
            queryset = queryset.order_by(ordering_param)
        else:
            queryset = queryset.order_by('-date_joined')

        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        if user.role == 'STAFF_VIEWER':
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Viewer staff accounts have read-only access.")

        parent = self.request.data.get('parent')
        from .models import is_company_user, NotificationType
        parent_instance = None
        if is_company_user(user) and parent:
            parent_instance = User.objects.filter(id=parent).first()
            serializer.save(parent=parent_instance, role=self.target_role)
        else:
            parent_instance = user
            serializer.save(parent=user, role=self.target_role)

        created_user = serializer.instance
        if parent_instance:
            send_system_notification(
                recipient=parent_instance,
                title="New Partner Added",
                message=f"New {created_user.get_role_display()} '{created_user.username}' has been onboarded into your network.",
                notification_type=NotificationType.PARTNER_ADDED,
                reference_id=created_user.id,
                sender=user
            )
            
        AuditLog.objects.create(
            user=user,
            action=f"CREATED_{self.target_role}",
            ip_address=get_client_ip(self.request),
            details={"created_username": serializer.instance.username}
        )

    def perform_update(self, serializer):
        user = self.request.user
        parent = self.request.data.get('parent')
        
        # Only SUPER_ADMIN can reassign parents
        if user.role == 'SUPER_ADMIN' and parent:
            parent_instance = User.objects.filter(id=parent).first()
            if parent_instance:
                serializer.save(parent=parent_instance)
            else:
                serializer.save()
        else:
            # If not SUPER_ADMIN, completely ignore any parent field sent in request
            # and just save the other fields
            serializer.save()
            
        AuditLog.objects.create(
            user=user,
            action=f"UPDATED_{self.target_role}",
            ip_address=get_client_ip(self.request),
            details={"updated_username": serializer.instance.username}
        )

    def destroy(self, request, *args, **kwargs):
        if request.user.role != 'SUPER_ADMIN':
            return Response(
                {"detail": "Only Super Admin can delete users."},
                status=status.HTTP_403_FORBIDDEN
            )
            
        instance = self.get_object()
        username = instance.username
        
        response = super().destroy(request, *args, **kwargs)
        
        AuditLog.objects.create(
            user=request.user,
            action=f"DELETED_{self.target_role}",
            ip_address=get_client_ip(self.request),
            details={"deleted_username": username}
        )
        return response

class SuperDistributorViewSet(BaseRoleViewSet):
    target_role = 'SUPER_DISTRIBUTOR'
    serializer_class = SuperDistributorSerializer

class DistributorViewSet(BaseRoleViewSet):
    target_role = 'DISTRIBUTOR'
    serializer_class = DistributorSerializer

class SubDistributorViewSet(BaseRoleViewSet):
    target_role = 'SUB_DISTRIBUTOR'
    serializer_class = SubDistributorSerializer

class RetailerViewSet(BaseRoleViewSet):
    target_role = 'RETAILER'
    serializer_class = RetailerSerializer

class CustomerViewSet(BaseRoleViewSet):
    target_role = 'CUSTOMER'
    serializer_class = CustomerSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        
        retailer_param = self.request.query_params.get('retailer')
        if retailer_param:
            if retailer_param.isdigit():
                queryset = queryset.filter(parent_id=retailer_param)
            else:
                from django.db.models import Q
                queryset = queryset.filter(
                    Q(parent__username__icontains=retailer_param) |
                    Q(parent__business_name__icontains=retailer_param)
                )

        emi_status = self.request.query_params.get('emi_status')
        if emi_status:
            emi_status = emi_status.lower()
            from emi.models import CustomerEMIContract, EMIContractStatus, EMIInstallment, InstallmentStatus
            from django.utils import timezone
            from django.db.models import Q
            today = timezone.localdate()

            if emi_status == 'no_emi':
                queryset = queryset.filter(emi_contracts__isnull=True)
            elif emi_status == 'completed':
                queryset = queryset.filter(emi_contracts__status=EMIContractStatus.COMPLETED).distinct()
            elif emi_status == 'overdue':
                overdue_cust_ids = EMIInstallment.objects.filter(
                    Q(status=InstallmentStatus.OVERDUE) |
                    Q(status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE], due_date__lt=today)
                ).values_list('contract__customer_id', flat=True)
                locked_cust_ids = CustomerEMIContract.objects.filter(
                    status=EMIContractStatus.LOCKED
                ).values_list('customer_id', flat=True)
                all_overdue_ids = set(overdue_cust_ids).union(set(locked_cust_ids))
                queryset = queryset.filter(id__in=all_overdue_ids)
            elif emi_status == 'on_emi':
                active_cust_ids = CustomerEMIContract.objects.filter(
                    status=EMIContractStatus.ACTIVE
                ).values_list('customer_id', flat=True)
                overdue_cust_ids = EMIInstallment.objects.filter(
                    Q(status=InstallmentStatus.OVERDUE) |
                    Q(status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE], due_date__lt=today)
                ).values_list('contract__customer_id', flat=True)
                clean_active_ids = set(active_cust_ids) - set(overdue_cust_ids)
                queryset = queryset.filter(id__in=clean_active_ids)

        return queryset

    @action(detail=False, methods=['get'], url_path='loan-providers')
    def loan_providers(self, request):
        """
        Returns all Loan Providers / Financiers matching the mobile app dropdown.
        """
        from devices.models import LoanProvider
        providers = [
            {"code": code, "name": label}
            for code, label in LoanProvider.choices
        ]
        return Response({"loan_providers": providers})

    @action(detail=False, methods=['post'], url_path='enroll')
    def enroll(self, request):
        """
        Step 1: Customer & Device Onboarding (Screen: Customer Device Details).
        Saves Customer basic info, photo, signature, Device IMEI, Loan Provider, 
        and activates Token Key. (EMI is attached in Step 2 via /api/devices/{id}/attach-emi/).
        """
        data = request.data
        user = request.user

        # 1. Customer Details
        full_name = data.get('full_name', '').strip()
        mobile_number = data.get('mobile_number', '').strip()
        if not full_name or not mobile_number:
            return Response(
                {"detail": "Fields 'full_name' and 'mobile_number' are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        name_parts = full_name.split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''

        email = data.get('email', '').strip() or f"{mobile_number}@customer.primelocker.com"
        password = data.get('password') or f"PL@{mobile_number[-4:]}"

        # 2. Device & Loan Details
        imei = data.get('imei', '').strip()
        if not imei:
            return Response(
                {"detail": "Field 'imei' is required for device enrollment."},
                status=status.HTTP_400_BAD_REQUEST
            )

        loan_provider = data.get('loan_provider', 'NO_LOAN_CASH')
        loan_provider_other = data.get('loan_provider_other', '')
        loan_account_number = data.get('loan_account_number', '')

        from devices.models import Device, DeviceCategory, OSType, DeviceScannerState, DeviceLockStatus, LockProvider
        from devices.imei_lookup import lookup_imei
        from keys.models import DeviceKey, KeyStatus, KeyAction, KeyTransaction

        detected = lookup_imei(imei)
        brand = data.get('brand') or detected.get('brand', 'Android Device')
        model = data.get('model') or detected.get('model', f"Model {imei[:8]}")
        device_category = data.get('device_category') or detected.get('device_category', DeviceCategory.MOBILE)
        os_type = data.get('os_type') or detected.get('os_type', OSType.ANDROID)
        device_state = data.get('device_state', DeviceScannerState.NEW)
        device_name = data.get('device_name') or f"{brand} {model}".strip()

        # Check existing IMEI
        if Device.objects.filter(imei=imei).exists():
            return Response(
                {"detail": f"A device with IMEI {imei} is already registered in the system."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 3. Check Activation Key if provided
        key_string = data.get('activation_key_string', '').strip()
        key_obj = None
        if key_string:
            key_obj = DeviceKey.objects.filter(key_string=key_string).first()
            if not key_obj:
                return Response({"detail": f"Activation key '{key_string}' not found."}, status=status.HTTP_404_NOT_FOUND)
            if key_obj.status != KeyStatus.AVAILABLE:
                return Response({"detail": f"Activation key '{key_string}' is already {key_obj.status}."}, status=status.HTTP_400_BAD_REQUEST)
            if user.role != 'SUPER_ADMIN' and key_obj.current_owner != user:
                return Response({"detail": "You do not own this activation key."}, status=status.HTTP_403_FORBIDDEN)

        # Item 12: Check Approved Phone Models (TAC list)
        tac = imei[:8]
        from devices.models import ApprovedDeviceTAC
        from accounts.models import PlatformSettings
        ps = PlatformSettings.get_settings()
        current_env = 'STAGING' if ('staging' in (ps.rocketpay_base_url or '').lower()) else 'PRODUCTION'
        
        if ApprovedDeviceTAC.objects.filter(environment__in=[current_env, 'ALL'], active=True).exists():
            is_approved = ApprovedDeviceTAC.objects.filter(
                tac=tac,
                environment__in=[current_env, 'ALL'],
                active=True
            ).exists()
            if not is_approved:
                return Response(
                    {"detail": f"Device model with TAC '{tac}' is not in the approved list for {current_env}. Enrolment rejected."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Determine Lock Provider
        lock_provider = data.get('lock_provider')
        if not lock_provider:
            lock_provider = LockProvider.ROCKETPAY if os_type == OSType.ANDROID else (
                LockProvider.APPLE_MDM if os_type == OSType.IOS else LockProvider.OWN_AGENT
            )

        # Item 9: Enrolment Order - save device first in atomic transaction, then enrol with RocketPay, roll back if fails
        with transaction.atomic():
            # Check/Create Customer
            customer = User.objects.filter(mobile_number=mobile_number).first()
            if not customer:
                customer = User.objects.create(
                    username=email,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    mobile_number=mobile_number,
                    alternate_mobile_number=data.get('alternate_mobile_number', ''),
                    address=data.get('address', ''),
                    city=data.get('city', ''),
                    state=data.get('state', ''),
                    pincode=data.get('pincode', ''),
                    role='CUSTOMER',
                    parent=user,
                    is_active=True
                )
                customer.set_password(password)

            # Handle files (photo, signature)
            if 'customer_photo' in request.FILES:
                customer.customer_photo = request.FILES['customer_photo']
            if 'customer_signature' in request.FILES:
                customer.customer_signature = request.FILES['customer_signature']
            customer.save()

            # Create Device in DB first
            device = Device.objects.create(
                customer=customer,
                retailer=user,
                device_category=device_category,
                os_type=os_type,
                device_state=device_state,
                imei=imei,
                secondary_imei=data.get('secondary_imei', ''),
                brand=brand,
                model=model,
                device_name=device_name,
                loan_provider=loan_provider,
                loan_provider_other=loan_provider_other,
                loan_account_number=loan_account_number,
                activation_key=key_obj,
                status=DeviceLockStatus.ACTIVE,
                lock_provider=lock_provider,
                provider_status='PENDING'
            )

            # Enrol with RocketPay if lock_provider is ROCKETPAY
            rp_device_id = None
            qr_code_data = None
            if lock_provider == LockProvider.ROCKETPAY:
                from devices.services.rocketpay_service import RocketPayService
                mdm_type = data.get('mdm_type', 'DLC').upper()
                frp_email = data.get('frp_email') or data.get('google_account_email', '')
                is_running = (device_state == DeviceScannerState.RUNNING)

                rp_result = RocketPayService.enroll_device(
                    device=device,
                    customer_name=full_name,
                    customer_mobile=mobile_number,
                    retailer_name=getattr(user, 'business_name', '') or user.username,
                    user=user,
                    mdm_type=mdm_type,
                    frp_email=frp_email,
                    is_running=is_running
                )
                if not rp_result.get('success'):
                    transaction.set_rollback(True)
                    return Response(
                        {"detail": f"RocketPay DLC enrollment failed: {rp_result.get('error')}. Device and customer registration rolled back."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                rp_device_id = rp_result.get('provider_device_id')
                qr_code_data = rp_result.get('qr_code_data')
                device.provider_device_id = rp_device_id
                device.provider_status = rp_result.get('provider_status', 'PENDING')
                device.save(update_fields=['provider_device_id', 'provider_status'])

            # Activate Key if present
            if key_obj:
                from django.utils import timezone
                key_obj.status = KeyStatus.ACTIVATED
                key_obj.activated_on_device_id = imei
                key_obj.activated_for_customer = customer
                key_obj.activated_at = timezone.now()
                key_obj.save()

                KeyTransaction.objects.create(
                    key=key_obj,
                    from_user=user,
                    to_user=customer,
                    action=KeyAction.ACTIVATED
                )

            AuditLog.objects.create(
                user=user,
                action="ENROLLED_CUSTOMER_DEVICE",
                ip_address=get_client_ip(request),
                details={
                    "customer_id": customer.id,
                    "device_id": device.id,
                    "imei": device.imei,
                    "loan_provider": loan_provider,
                    "lock_provider": lock_provider,
                    "provider_device_id": rp_device_id
                }
            )

        photo_url = request.build_absolute_uri(customer.customer_photo.url) if customer.customer_photo else None
        sig_url = request.build_absolute_uri(customer.customer_signature.url) if customer.customer_signature else None

        return Response({
            "message": "Customer and device successfully enrolled. You can now attach an EMI plan in the next step.",
            "customer": {
                "id": customer.id,
                "name": f"{customer.first_name} {customer.last_name}".strip(),
                "mobile_number": customer.mobile_number,
                "alternate_mobile_number": customer.alternate_mobile_number,
                "email": customer.email,
                "customer_photo": photo_url,
                "customer_signature": sig_url,
            },
            "device": {
                "id": device.id,
                "imei": device.imei,
                "brand": device.brand,
                "model": device.model,
                "device_category": device.device_category,
                "os_type": device.os_type,
                "device_state": device.device_state,
                "loan_provider": device.loan_provider,
                "loan_provider_display": device.get_loan_provider_display(),
                "loan_account_number": device.loan_account_number,
                "status": device.status,
                "lock_provider": device.lock_provider,
                "provider_device_id": device.provider_device_id,
                "provider_status": device.provider_status,
                "qr_code_data": qr_code_data,
                "offline_unlock_code": device.offline_unlock_code,
                "activation_key": key_obj.key_string if key_obj else None
            },
            "next_step": {
                "action": "ATTACH_EMI_PLAN",
                "endpoint": f"/api/devices/{device.id}/attach-emi/",
                "method": "POST"
            }
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='reset-password')
    def reset_password(self, request, pk=None):
        """
        Store / Retailer resets a customer's password (POST /api/customers/{id}/reset-password/).
        Accessible by Retailer (owner of the customer), upline Distributors, or Super Admin/Staff.
        """
        customer = self.get_object()
        return self._do_customer_password_reset(customer, request)

    @action(detail=False, methods=['post'], url_path='reset-password')
    def reset_customer_password(self, request):
        """
        Store / Retailer resets customer password by mobile or customer_id (POST /api/customers/reset-password/).
        Accepts: customer_id, mobile_number, or username in request body.
        """
        customer_id = request.data.get('customer_id') or request.data.get('id')
        mobile_number = request.data.get('mobile_number', '').strip()
        username = request.data.get('username', '').strip()

        customer = None
        if customer_id:
            customer = User.objects.filter(id=customer_id, role='CUSTOMER').first()
        elif mobile_number:
            customer = User.objects.filter(mobile_number=mobile_number, role='CUSTOMER').first()
            if not customer and mobile_number.startswith('+91'):
                customer = User.objects.filter(mobile_number=mobile_number[3:], role='CUSTOMER').first()
            elif not customer and len(mobile_number) == 10 and mobile_number.isdigit():
                customer = User.objects.filter(mobile_number=f"+91{mobile_number}", role='CUSTOMER').first()
        elif username:
            customer = User.objects.filter(username=username, role='CUSTOMER').first()

        if not customer:
            return Response(
                {"error": "Customer account not found. Please provide a valid customer_id, mobile_number, or username."},
                status=status.HTTP_404_NOT_FOUND
            )

        return self._do_customer_password_reset(customer, request)

    def _do_customer_password_reset(self, customer, request):
        user = request.user

        # 1. Role / Hierarchy check
        if user.role == 'CUSTOMER':
            return Response(
                {"detail": "Customers do not have permission to reset passwords for other accounts."},
                status=status.HTTP_403_FORBIDDEN
            )
        if user.role == 'STAFF_VIEWER':
            return Response(
                {"detail": "Viewer staff accounts have read-only access."},
                status=status.HTTP_403_FORBIDDEN
            )

        # If user is RETAILER, customer must be enrolled with or owned by this retailer
        if user.role == 'RETAILER':
            is_parent = (customer.parent_id == user.id)
            has_contract = customer.emi_contracts.filter(retailer=user).exists()
            from devices.models import Device
            has_device = Device.objects.filter(customer=customer, retailer=user).exists()
            if not (is_parent or has_contract or has_device):
                return Response(
                    {"detail": "You do not have permission to reset the password for this customer (not registered under your store)."},
                    status=status.HTTP_403_FORBIDDEN
                )
        elif not is_company_user(user):
            # Check downline hierarchy for distributors
            from accounts.permissions import is_downstream_user
            if not is_downstream_user(user, customer):
                return Response(
                    {"detail": "You do not have permission to manage this customer."},
                    status=status.HTTP_403_FORBIDDEN
                )

        # 2. Determine new password
        # If provided in request body, use it; otherwise generate default PL@{mobile[-4:]} or 4-digit PIN
        new_password = request.data.get('new_password', '').strip()
        if not new_password:
            mob = str(customer.mobile_number or '').strip()
            if len(mob) >= 4:
                new_password = f"PL@{mob[-4:]}"
            else:
                import random
                new_password = f"PL@{random.randint(1000, 9999)}"

        customer.set_password(new_password)
        customer.save(update_fields=['password'])

        AuditLog.objects.create(
            user=user,
            action="STORE_RESET_CUSTOMER_PASSWORD",
            ip_address=get_client_ip(request),
            details={
                "customer_id": customer.id,
                "customer_username": customer.username,
                "customer_mobile": customer.mobile_number,
                "reset_by": user.username,
                "reset_by_role": user.role
            }
        )

        # Send WhatsApp notification if customer mobile exists
        try:
            from accounts.whatsapp import WhatsAppService
            if customer.mobile_number:
                WhatsAppService.send_template(
                    phone_number=customer.mobile_number,
                    template_name='password_reset_by_store',
                    parameters={
                        'customer_name': customer.first_name or customer.username,
                        'new_password': new_password
                    },
                    recipient_name=customer.first_name
                )
        except Exception:
            pass

        return Response({
            "success": True,
            "message": f"Password for customer '{customer.first_name or customer.username}' has been successfully reset.",
            "customer_id": customer.id,
            "customer_name": f"{customer.first_name} {customer.last_name}".strip() or customer.username,
            "mobile_number": customer.mobile_number,
            "temporary_password": new_password
        }, status=status.HTTP_200_OK)

class PasswordResetRequestView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            user = User.objects.filter(username=username).first() or \
                   User.objects.filter(email=username).first() or \
                   User.objects.filter(mobile_number=username).first()
            
            if user:
                # Generate 6 digit OTP
                otp_code = str(random.randint(100000, 999999))
                PasswordResetOTP.objects.create(user=user, otp=otp_code)
                
                # TODO: Integrate real Email/SMS provider here
                print(f"--- OTP for {user.username} is {otp_code} ---")
                
                return Response({
                    "message": "If the account exists, an OTP has been sent."
                }, status=status.HTTP_200_OK)
            return Response({"message": "If the account exists, an OTP has been sent."}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            otp_code = serializer.validated_data['otp']
            new_password = serializer.validated_data['new_password']

            user = User.objects.filter(username=username).first() or \
                   User.objects.filter(email=username).first() or \
                   User.objects.filter(mobile_number=username).first()

            if not user:
                return Response({"error": "Invalid details"}, status=status.HTTP_400_BAD_REQUEST)

            otp_record = PasswordResetOTP.objects.filter(user=user, otp=otp_code).order_by('-created_at').first()
            
            if otp_record and otp_record.is_valid():
                user.set_password(new_password)
                user.save()
                otp_record.is_used = True
                otp_record.save()
                
                AuditLog.objects.create(
                    user=user,
                    action="PASSWORD_RESET",
                    ip_address=get_client_ip(request)
                )
                
                return Response({"message": "Password reset successful."}, status=status.HTTP_200_OK)
            
            return Response({"error": "Invalid or expired OTP"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def get_all_downstream_users(user):
    """Returns a flat list of all users in this user's downstream hierarchy."""
    if user.role == 'SUPER_ADMIN':
        return list(User.objects.exclude(id=user.id))
    subordinates = list(user.children.all())
    for sub in subordinates:
        subordinates.extend(get_all_downstream_users(sub))
    return subordinates


class DashboardView(APIView):
    """
    Phase 5B: Main Executive & Role-Aware Overview Dashboard.
    Returns Network KPIs, Device/EMI KPIs, Financial Collections, Key Inventory, and Recent Activity.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.db.models import Sum, Count, Q
        from django.utils import timezone
        from decimal import Decimal
        from keys.models import DeviceKey, KeyStatus, KeyRequest, KeyRequestStatus
        from emi.models import CustomerEMIContract, EMIInstallment, InstallmentStatus, EMIContractStatus

        user = request.user
        today = timezone.localdate()

        # 1. Network / Hierarchy scoping
        if user.role == 'SUPER_ADMIN':
            downline_qs = User.objects.all()
            retailer_ids = list(User.objects.filter(role='RETAILER').values_list('id', flat=True))
        elif user.role == 'RETAILER':
            downline_qs = User.objects.filter(parent=user)
            retailer_ids = [user.id]
        else:
            downline_users = get_all_downstream_users(user)
            downline_ids = [u.id for u in downline_users]
            downline_qs = User.objects.filter(id__in=downline_ids)
            retailer_ids = [u.id for u in downline_users if u.role == 'RETAILER']

        network_kpis = {
            "super_distributors": downline_qs.filter(role='SUPER_DISTRIBUTOR').count(),
            "distributors": downline_qs.filter(role='DISTRIBUTOR').count(),
            "sub_distributors": downline_qs.filter(role='SUB_DISTRIBUTOR').count(),
            "retailers": downline_qs.filter(role='RETAILER').count(),
            "customers": downline_qs.filter(role='CUSTOMER').count(),
            "active_users": downline_qs.filter(is_active=True).count(),
            "inactive_users": downline_qs.filter(is_active=False).count(),
        }

        # 2. Device & EMI Status KPIs
        contracts_qs = CustomerEMIContract.objects.filter(retailer_id__in=retailer_ids)
        total_devices = contracts_qs.count()
        active_devices = contracts_qs.filter(status=EMIContractStatus.ACTIVE).count()
        locked_devices = contracts_qs.filter(status=EMIContractStatus.LOCKED).count()
        completed_contracts = contracts_qs.filter(status=EMIContractStatus.COMPLETED).count()

        device_and_emi_kpis = {
            "total_registered_devices": total_devices,
            "active_unlocked_devices": active_devices,
            "locked_devices": locked_devices,
            "completed_emi_contracts": completed_contracts,
        }

        # 3. Financial & Collection KPIs
        from datetime import timedelta
        range_param = request.query_params.get('range', '').lower()
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        start_date = None
        end_date = today

        if range_param == 'today':
            start_date = today
            end_date = today
        elif range_param == '7d':
            start_date = today - timedelta(days=7)
            end_date = today
        elif range_param == '30d':
            start_date = today - timedelta(days=30)
            end_date = today
        elif range_param == 'this_month':
            start_date = today.replace(day=1)
            end_date = today
        elif start_date_str:
            try:
                start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                start_date = None
            if end_date_str:
                try:
                    end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
                except ValueError:
                    end_date = today

        installments_qs = EMIInstallment.objects.filter(contract__retailer_id__in=retailer_ids)

        # Auto-mark overdue installments where due_date < today
        installments_qs.filter(
            status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE],
            due_date__lt=today
        ).update(status=InstallmentStatus.OVERDUE)

        total_financed = contracts_qs.aggregate(total=Sum('financed_amount'))['total'] or Decimal('0.00')
        paid_qs = installments_qs.filter(status=InstallmentStatus.PAID)
        total_collected = paid_qs.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        today_collected = paid_qs.filter(collection_date__date=today).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        month_collected = paid_qs.filter(
            collection_date__year=today.year,
            collection_date__month=today.month
        ).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')

        if start_date:
            range_collected = paid_qs.filter(
                collection_date__date__gte=start_date,
                collection_date__date__lte=end_date
            ).aggregate(total=Sum('paid_amount'))['total'] or Decimal('0.00')
        else:
            range_collected = total_collected

        overdue_qs = installments_qs.filter(status=InstallmentStatus.OVERDUE)
        total_overdue_amount = overdue_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        overdue_count = overdue_qs.count()

        financial_kpis = {
            "total_financed_amount": total_financed,
            "total_collected_amount": total_collected,
            "range_collected_amount": range_collected,
            "total_pending_amount": max(total_financed - total_collected, Decimal('0.00')),
            "today_collection_amount": today_collected,
            "this_month_collection_amount": month_collected,
            "total_overdue_amount": total_overdue_amount,
            "overdue_installments_count": overdue_count,
            "date_filter": {
                "start_date": str(start_date) if start_date else None,
                "end_date": str(end_date) if start_date else None,
                "range": range_param or None
            }
        }

        # 4. Key Inventory KPIs (Owned by current user)
        owned_keys = DeviceKey.objects.filter(current_owner=user)
        available_keys = owned_keys.filter(status=KeyStatus.AVAILABLE).count()
        activated_keys = owned_keys.filter(status=KeyStatus.ACTIVATED).count()
        revoked_keys = owned_keys.filter(status=KeyStatus.REVOKED).count()

        by_type = list(
            owned_keys.values('device_type', 'status')
            .annotate(count=Count('id'))
            .order_by('device_type', 'status')
        )
        pending_requests = KeyRequest.objects.filter(parent=user, status=KeyRequestStatus.PENDING).count()

        key_inventory_kpis = {
            "available_keys": available_keys,
            "activated_keys": activated_keys,
            "revoked_keys": revoked_keys,
            "pending_key_requests_received": pending_requests,
            "by_device_type": by_type
        }

        # 5. Recent Payments (Latest 5)
        recent_payments_qs = paid_qs.select_related('contract__customer', 'contract__retailer').order_by('-collection_date')[:5]
        recent_payments = []
        for p in recent_payments_qs:
            c = p.contract.customer
            r = p.contract.retailer
            c_name = f"{c.first_name} {c.last_name}".strip() or c.username
            r_store = r.business_name or f"{r.first_name} {r.last_name}".strip() or r.username
            recent_payments.append({
                "installment_id": p.id,
                "contract_id": p.contract_id,
                "customer_name": c_name,
                "store_name": r_store,
                "amount": p.paid_amount,
                "payment_mode": p.payment_mode,
                "collection_date": p.collection_date
            })

        # 6. Recent Audit Activity (Latest 10)
        if user.role == 'SUPER_ADMIN':
            recent_logs_qs = AuditLog.objects.select_related('user').order_by('-timestamp')[:10]
        else:
            allowed_user_ids = [user.id] + list(downline_qs.values_list('id', flat=True))
            recent_logs_qs = AuditLog.objects.filter(user_id__in=allowed_user_ids).select_related('user').order_by('-timestamp')[:10]

        recent_activity = AuditLogSerializer(recent_logs_qs, many=True).data

        user_name = f"{user.first_name} {user.last_name}".strip() or user.username

        # RP11: RocketPay Wallet Key Balance for Super Admin & Company Staff
        rocketpay_wallet = None
        from accounts.models import is_company_user
        if is_company_user(user):
            from devices.services.rocketpay_service import RocketPayService
            try:
                rocketpay_wallet = RocketPayService.get_wallet_balance()
            except Exception:
                rocketpay_wallet = {"outstanding": 0, "low_balance_warning": True}

        return Response({
            "user_profile": {
                "id": user.id,
                "name": user_name,
                "username": user.username,
                "role": user.role,
                "business_name": user.business_name
            },
            "network_kpis": network_kpis,
            "device_and_emi_kpis": device_and_emi_kpis,
            "financial_kpis": financial_kpis,
            "key_inventory_kpis": key_inventory_kpis,
            "rocketpay_wallet": rocketpay_wallet,
            "recent_payments": recent_payments,
            "recent_activity": recent_activity
        })


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Phase 5B: Dedicated Audit Logs Viewer (Sidebar -> Audit Logs).
    Supports pagination, filtering by action/role/user/date/search, and ?export=csv.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        from django.db.models import Q
        user = self.request.user

        if user.role == 'SUPER_ADMIN':
            qs = AuditLog.objects.select_related('user').order_by('-timestamp')
        else:
            downline_users = get_all_downstream_users(user)
            allowed_ids = [user.id] + [u.id for u in downline_users]
            qs = AuditLog.objects.filter(user_id__in=allowed_ids).select_related('user').order_by('-timestamp')

        # Filters
        action_filter = self.request.query_params.get('action')
        if action_filter:
            qs = qs.filter(action__icontains=action_filter)

        user_id = self.request.query_params.get('user_id')
        if user_id:
            qs = qs.filter(user_id=user_id)

        role_filter = self.request.query_params.get('role')
        if role_filter:
            qs = qs.filter(user__role=role_filter.upper())

        start_date = self.request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(timestamp__date__gte=start_date)

        end_date = self.request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(timestamp__date__lte=end_date)

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(action__icontains=search) |
                Q(user__username__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__business_name__icontains=search) |
                Q(ip_address__icontains=search)
            )

        return qs

    def list(self, request, *args, **kwargs):
        if request.query_params.get('export') == 'csv':
            from reports.utils import generate_csv_response
            qs = self.filter_queryset(self.get_queryset())
            headers = ["Log ID", "Timestamp", "User", "Role", "Business Name", "Action", "IP Address", "Details"]
            rows = []
            for log in qs[:5000]:
                u = log.user
                uname = (f"{u.first_name} {u.last_name}".strip() or u.username) if u else "System"
                urole = u.role if u else "N/A"
                ubiz = (u.business_name or "") if u else ""
                rows.append([
                    log.id,
                    log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    uname,
                    urole,
                    ubiz,
                    log.action,
                    log.ip_address or "",
                    str(log.details or {})
                ])
            return generate_csv_response("audit_logs_report.csv", headers, rows)

        return super().list(request, *args, **kwargs)


class CurrentUserView(APIView):
    """
    P1 Endpoint: GET /api/auth/me/
    Returns the authenticated user's profile, role, parent, key counts,
    and permissions. Used by the web panel to restore session on page reload.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        full_name = f"{user.first_name} {user.last_name}".strip()
        
        parent_data = None
        if user.parent:
            p_name = f"{user.parent.first_name} {user.parent.last_name}".strip() or user.parent.username
            parent_data = {
                "id": user.parent.id,
                "name": p_name,
                "role": user.parent.role,
                "business_name": user.parent.business_name
            }

        # Available keys breakdown
        from keys.models import DeviceKey, KeyStatus
        from django.db.models import Count
        owned_keys = DeviceKey.objects.filter(current_owner=user, status=KeyStatus.AVAILABLE)
        by_type = dict(owned_keys.values('device_type').annotate(c=Count('id')).values_list('device_type', 'c'))
        by_type['total'] = sum(by_type.values())

        # Children counts
        subordinates = user.get_all_subordinates()
        children_counts = {
            'distributors': sum(1 for u in subordinates if u.role == 'DISTRIBUTOR'),
            'sub_distributors': sum(1 for u in subordinates if u.role == 'SUB_DISTRIBUTOR'),
            'retailers': sum(1 for u in subordinates if u.role == 'RETAILER'),
            'customers': sum(1 for u in subordinates if u.role == 'CUSTOMER'),
            'total': len(subordinates)
        }

        logo_url = None
        if user.logo:
            logo_url = request.build_absolute_uri(user.logo.url) if request else user.logo.url

        return Response({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "name": full_name or user.business_name or user.username,
            "role": user.role,
            "mobile_number": user.mobile_number,
            "alternate_mobile_number": user.alternate_mobile_number,
            "business_name": user.business_name,
            "store_phone": user.store_phone,
            "logo": logo_url,
            "state": user.state,
            "city": user.city,
            "pincode": user.pincode,
            "address": user.address,
            "pan_number": user.pan_number,
            "gst_number": user.gst_number,
            "can_reclaim_keys": user.can_reclaim_keys,
            "is_active": user.is_active,
            "date_joined": user.date_joined,
            "parent": parent_data,
            "available_keys": by_type,
            "children_counts": children_counts,
            "permissions": {
                "is_super_admin": user.role == 'SUPER_ADMIN',
                "can_lock_devices": user.role != 'CUSTOMER',
                "can_generate_keys": user.role == 'SUPER_ADMIN',
                "can_reclaim_keys": user.role == 'SUPER_ADMIN' or user.can_reclaim_keys,
                "can_add_partners": user.can_add_partners,
                "can_see_devices": user.can_see_devices,
                "can_see_customer_phones": user.can_see_customer_phones,
            },
            "approval_step": user.approval_step,
            "suspend_reason": user.suspend_reason,
        })

    def patch(self, request):
        """
        Partner Business Profile Update (Item 14: PATCH /api/auth/me/).
        Allows updating name, business_name, address, city, store_phone, and logo.
        """
        user = request.user
        data = request.data

        if 'name' in data:
            name_val = str(data['name']).strip()
            parts = name_val.split(' ', 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ''
        elif 'first_name' in data:
            user.first_name = str(data['first_name']).strip()
        if 'last_name' in data:
            user.last_name = str(data['last_name']).strip()

        if 'business_name' in data:
            user.business_name = str(data['business_name']).strip()
        if 'address' in data:
            user.address = str(data['address']).strip()
        if 'city' in data:
            user.city = str(data['city']).strip()
        if 'state' in data:
            user.state = str(data['state']).strip()
        if 'pincode' in data:
            user.pincode = str(data['pincode']).strip()
        if 'store_phone' in data:
            user.store_phone = str(data['store_phone']).strip()

        if 'logo' in request.FILES:
            user.logo = request.FILES['logo']

        user.save()

        AuditLog.objects.create(
            user=user,
            action="UPDATED_BUSINESS_PROFILE",
            ip_address=get_client_ip(request),
            details={"updated_fields": list(data.keys())}
        )

        return self.get(request)



class LogoutView(APIView):
    """
    POST /api/auth/logout/
    Blacklists the supplied JWT refresh token and logs the user logout.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({"detail": "Refresh token is required in 'refresh' field."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            from rest_framework_simplejwt.tokens import RefreshToken, TokenError
            token = RefreshToken(refresh_token)
            token.blacklist()
            AuditLog.objects.create(
                user=request.user,
                action="USER_LOGOUT",
                ip_address=get_client_ip(request),
                details={"method": "jwt_blacklist"}
            )
            return Response({"detail": "Successfully logged out."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"detail": str(e) if str(e) else "Invalid token or token already blacklisted."}, status=status.HTTP_400_BAD_REQUEST)


class ChangePasswordView(APIView):
    """
    Screen A-42: POST /api/auth/change-password/
    Allows the logged-in user to change their password securely.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .serializers import ChangePasswordSerializer
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = request.user
            user.set_password(serializer.validated_data['new_password'])
            user.save()
            AuditLog.objects.create(
                user=user,
                action="PASSWORD_CHANGED",
                ip_address=get_client_ip(request),
                details={"method": "profile_self_service"}
            )
            return Response({"detail": "Password updated successfully."}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ProtectedMediaView(APIView):
    """
    Bug 3 Fix: Authenticated & Hierarchy-Scoped Media File Server.
    Protects /media/kyc_documents/, /media/customer_photos/, /media/customer_signatures/
    so unauthenticated or unauthorized users cannot access sensitive customer KYC data.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, file_path):
        clean_path = os.path.normpath(file_path).lstrip('/\\')
        full_path = os.path.abspath(os.path.join(settings.MEDIA_ROOT, clean_path))
        media_root_abs = os.path.abspath(str(settings.MEDIA_ROOT))

        if not full_path.startswith(media_root_abs):
            return Response({"detail": "Access denied."}, status=status.HTTP_403_FORBIDDEN)

        if not os.path.exists(full_path):
            raise Http404("File not found.")

        # Super Admin has global access to all files
        user = request.user
        if user.role != 'SUPER_ADMIN':
            target_user = User.objects.filter(
                models.Q(kyc_document__endswith=clean_path) |
                models.Q(customer_photo__endswith=clean_path) |
                models.Q(customer_signature__endswith=clean_path) |
                models.Q(id_front_image__endswith=clean_path) |
                models.Q(id_back_image__endswith=clean_path)
            ).first()

            if target_user:
                if target_user != user and target_user not in user.get_all_subordinates():
                    return Response(
                        {"detail": "You do not have permission to access this document."},
                        status=status.HTTP_403_FORBIDDEN
                    )

        content_type, _ = mimetypes.guess_type(full_path)
        return FileResponse(open(full_path, 'rb'), content_type=content_type or 'application/octet-stream')


def send_system_notification(recipient, title, message, notification_type='SYSTEM', reference_id=None, sender=None):
    from .models import Notification
    return Notification.objects.create(
        recipient=recipient,
        sender=sender,
        title=title,
        message=message,
        notification_type=notification_type,
        reference_id=str(reference_id) if reference_id else None
    )


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Screen A-41 Bell Icon Notifications.
    Lists alerts, supports unread count, and mark-read actions.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import NotificationSerializer
        return NotificationSerializer

    def get_queryset(self):
        from .models import Notification
        qs = Notification.objects.filter(recipient=self.request.user)
        if self.request.query_params.get('unread') == 'true':
            qs = qs.filter(is_read=False)
        notif_type = self.request.query_params.get('type')
        if notif_type:
            qs = qs.filter(notification_type__iexact=notif_type)
        return qs

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        from .models import Notification
        count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return Response({"unread_count": count})

    @action(detail=True, methods=['post'], url_path='mark-read')
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.save(update_fields=['is_read'])
        return Response({"status": "marked_as_read", "id": notif.id})

    @action(detail=False, methods=['post'], url_path='mark-all-read')
    def mark_all_read(self, request):
        from .models import Notification
        updated = Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({"status": "all_marked_as_read", "updated_count": updated})


class PlatformSettingsView(APIView):
    """
    Screen A-39 / A-40: Company & System Settings.
    GET: View company profile, support contacts, and grace period settings.
    PATCH / PUT: Super Admin updates system configurations.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import PlatformSettings
        from .serializers import PlatformSettingsSerializer
        settings_obj = PlatformSettings.get_settings()
        serializer = PlatformSettingsSerializer(settings_obj, context={'request': request})
        return Response(serializer.data)

    def patch(self, request):
        if request.user.role != 'SUPER_ADMIN':
            return Response({"detail": "Only Super Admin can update system settings."}, status=status.HTTP_403_FORBIDDEN)
        from .models import PlatformSettings
        from .serializers import PlatformSettingsSerializer
        settings_obj = PlatformSettings.get_settings()
        serializer = PlatformSettingsSerializer(settings_obj, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            AuditLog.objects.create(
                user=request.user,
                action="UPDATED_SYSTEM_SETTINGS",
                ip_address=get_client_ip(request),
                details=request.data
            )
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        return self.patch(request)


class StaffViewSet(viewsets.ModelViewSet):
    """
    Super Admin only: Manage internal company staff roles (Operations, Collections, Support, Viewer).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        from .serializers import StaffUserSerializer
        return StaffUserSerializer

    def get_queryset(self):
        if self.request.user.role != 'SUPER_ADMIN':
            return User.objects.none()
        return User.objects.filter(role__in=[
            'STAFF_OPERATIONS', 'STAFF_COLLECTIONS', 'STAFF_SUPPORT', 'STAFF_VIEWER'
        ]).order_by('-date_joined')

    def perform_create(self, serializer):
        from rest_framework import exceptions
        if self.request.user.role != 'SUPER_ADMIN':
            raise exceptions.PermissionDenied("Only Super Admin can create staff accounts.")
        instance = serializer.save()
        AuditLog.objects.create(
            user=self.request.user,
            action="CREATED_STAFF_MEMBER",
            ip_address=get_client_ip(self.request),
            details={"staff_id": instance.id, "email": instance.email, "role": instance.role}
        )

    def perform_destroy(self, instance):
        from rest_framework import exceptions
        if self.request.user.role != 'SUPER_ADMIN':
            raise exceptions.PermissionDenied("Only Super Admin can remove staff.")
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        AuditLog.objects.create(
            user=self.request.user,
            action="DEACTIVATED_STAFF_MEMBER",
            ip_address=get_client_ip(self.request),
            details={"staff_id": instance.id, "email": instance.email}
        )


class WhatsAppCampaignView(APIView):
    """
    WhatsApp Automated Messaging & Broadcast Campaigns.
    GET: View available message templates and recent delivery logs.
    POST: Super Admin / Collections Staff triggers batch WhatsApp messages.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import WhatsAppMessageLog
        from .serializers import WhatsAppMessageLogSerializer
        from .whatsapp import WhatsAppService
        recent_logs = WhatsAppMessageLog.objects.all()[:50]
        serializer = WhatsAppMessageLogSerializer(recent_logs, many=True)
        return Response({
            "templates": WhatsAppService.TEMPLATES,
            "recent_deliveries": serializer.data
        })

    def post(self, request):
        user = request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS', 'STAFF_COLLECTIONS']:
            return Response({"detail": "You do not have permission to launch WhatsApp campaigns."}, status=status.HTTP_403_FORBIDDEN)

        from .whatsapp import WhatsAppService
        template_name = request.data.get('template_name')
        if not template_name or template_name not in WhatsAppService.TEMPLATES:
            return Response({
                "detail": f"Valid 'template_name' is required. Choices: {list(WhatsAppService.TEMPLATES.keys())}"
            }, status=status.HTTP_400_BAD_REQUEST)

        segment = request.data.get('segment', 'CUSTOM')
        phone_numbers = request.data.get('phone_numbers', [])
        params = request.data.get('parameters', {})

        from emi.models import CustomerEMIContract, EMIContractStatus
        recipients = []
        if segment == 'OVERDUE_ALL':
            from emi.models import EMIInstallment, InstallmentStatus
            from django.utils import timezone
            today = timezone.localdate()
            EMIInstallment.objects.filter(
                status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE],
                due_date__lt=today
            ).update(status=InstallmentStatus.OVERDUE)

            overdue_insts = EMIInstallment.objects.filter(
                status=InstallmentStatus.OVERDUE
            ).select_related('contract__customer')

            seen_phones = set()
            for inst in overdue_insts:
                c = inst.contract.customer
                if c and c.mobile_number and c.mobile_number not in seen_phones:
                    seen_phones.add(c.mobile_number)
                    name = f"{c.first_name} {c.last_name}".strip() or c.username
                    recipients.append((c.mobile_number, name))
        elif segment == 'LOCKED_DEVICES':
            locked_contracts = CustomerEMIContract.objects.filter(status=EMIContractStatus.LOCKED).select_related('customer')
            for c in locked_contracts:
                if c.customer and c.customer.mobile_number:
                    recipients.append((c.customer.mobile_number, c.customer.get_full_name() or c.customer.username))
        elif phone_numbers:
            for p in phone_numbers:
                recipients.append((p, 'Customer'))

        if not recipients:
            return Response({"detail": "No recipients found for the selected segment."}, status=status.HTTP_400_BAD_REQUEST)

        results = []
        for phone, name in recipients:
            p_copy = params.copy()
            p_copy.setdefault('customer_name', name)
            res = WhatsAppService.send_template(phone, template_name, p_copy, recipient_name=name)
            results.append({'phone': phone, 'status': res.get('status')})

        AuditLog.objects.create(
            user=user,
            action="LAUNCHED_WHATSAPP_CAMPAIGN",
            ip_address=get_client_ip(request),
            details={"template": template_name, "segment": segment, "recipient_count": len(recipients)}
        )

        return Response({
            "message": f"WhatsApp campaign '{template_name}' dispatched successfully to {len(recipients)} recipients.",
            "total_recipients": len(recipients),
            "results": results[:20]
        })


class BannerViewSet(viewsets.ModelViewSet):
    """
    Screen A-42 Banners Management.
    GET /api/banners/ : lists active banners for user's role (or all banners for admins/staff).
    POST, PUT, PATCH, DELETE : managed by Super Admin and Staff Operations.
    """
    from .models import Banner
    from .serializers import BannerSerializer
    queryset = Banner.objects.all()
    serializer_class = BannerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        from .models import is_company_user
        if is_company_user(user):
            return self.queryset.all()
        return self.queryset.filter(
            is_active=True,
            target_role__in=['ALL', getattr(user, 'role', 'ALL')]
        )

    def perform_create(self, serializer):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can manage banners.")
        serializer.save(created_by=user)

    def perform_update(self, serializer):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can manage banners.")
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only Super Admin or Operations Staff can manage banners.")
        instance.delete()



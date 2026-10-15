from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from django.db import transaction
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

    def get_queryset(self):
        user = self.request.user
        queryset = User.objects.filter(role=self.target_role)
        
        if user.role == 'SUPER_ADMIN':
            return queryset
            
        return queryset.filter(parent=user)

    def perform_create(self, serializer):
        user = self.request.user
        parent = self.request.data.get('parent')
        
        if user.role == 'SUPER_ADMIN' and parent:
            parent_instance = User.objects.filter(id=parent).first()
            serializer.save(parent=parent_instance, role=self.target_role)
        else:
            serializer.save(parent=user, role=self.target_role)
            
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

        from devices.models import Device, DeviceCategory, OSType, DeviceScannerState, DeviceLockStatus
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

            # Create Device
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
                status=DeviceLockStatus.ACTIVE
            )

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
                    "loan_provider": loan_provider
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
                "offline_unlock_code": device.offline_unlock_code,
                "activation_key": key_obj.key_string if key_obj else None
            },
            "next_step": {
                "action": "ATTACH_EMI_PLAN",
                "endpoint": f"/api/devices/{device.id}/attach-emi/",
                "method": "POST"
            }
        }, status=status.HTTP_201_CREATED)

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

        overdue_qs = installments_qs.filter(status=InstallmentStatus.OVERDUE)
        total_overdue_amount = overdue_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        overdue_count = overdue_qs.count()

        financial_kpis = {
            "total_financed_amount": total_financed,
            "total_collected_amount": total_collected,
            "total_pending_amount": max(total_financed - total_collected, Decimal('0.00')),
            "today_collection_amount": today_collected,
            "this_month_collection_amount": month_collected,
            "total_overdue_amount": total_overdue_amount,
            "overdue_installments_count": overdue_count,
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


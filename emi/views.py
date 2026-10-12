from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction, models
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from .models import (
    EMIPlanTemplate,
    CustomerEMIContract,
    EMIInstallment,
    InstallmentStatus,
    EMIContractStatus,
    PaymentMode
)
from .serializers import (
    EMIPlanTemplateSerializer,
    CustomerEMIContractSerializer,
    EMIInstallmentSerializer,
    RecordPaymentSerializer,
    OverdueCustomerItemSerializer
)


class IsSuperAdminOrReadOnly(permissions.BasePermission):
    """
    Super Admin can perform any action.
    Other authenticated users have read-only access.
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.role == 'SUPER_ADMIN'


class EMIPlanTemplateViewSet(viewsets.ModelViewSet):
    """
    CRUD for Master EMI Plans (Screen A-19).
    Only Super Admin can create, edit, deactivate plans.
    Retailers/Distributors can view active plans.
    """
    permission_classes = [IsSuperAdminOrReadOnly]
    serializer_class = EMIPlanTemplateSerializer

    def get_queryset(self):
        user = self.request.user
        if user.role == 'SUPER_ADMIN':
            return EMIPlanTemplate.objects.all()
        # Non-superadmins only see active plans
        return EMIPlanTemplate.objects.filter(is_active=True)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        instance = serializer.save()
        from accounts.models import AuditLog
        from accounts.views import get_client_ip
        AuditLog.objects.create(
            user=self.request.user,
            action="UPDATED_EMI_PLAN",
            ip_address=get_client_ip(self.request),
            details={"plan_id": instance.id, "plan_name": instance.name}
        )

    @action(detail=True, methods=['post'], url_path='toggle-status')
    def toggle_status(self, request, pk=None):
        """Toggle a plan between active and inactive"""
        if request.user.role != 'SUPER_ADMIN':
            return Response(
                {"detail": "Only Super Admin can activate or deactivate EMI plans."},
                status=status.HTTP_403_FORBIDDEN
            )
        plan = self.get_object()
        plan.is_active = not plan.is_active
        plan.save()
        return Response({
            "message": f"Plan '{plan.name}' is now {'active' if plan.is_active else 'inactive'}.",
            "is_active": plan.is_active
        })


class CustomerEMIContractViewSet(viewsets.ModelViewSet):
    """
    Manages EMI contracts (Screen A-20 Set up EMI).
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CustomerEMIContractSerializer

    def get_queryset(self):
        user = self.request.user
        if user.role == 'SUPER_ADMIN':
            return CustomerEMIContract.objects.all()
        elif user.role == 'RETAILER':
            return CustomerEMIContract.objects.filter(retailer=user)
        elif user.role == 'CUSTOMER':
            return CustomerEMIContract.objects.filter(customer=user)
        else:
            # Super Distributor, Distributor, Sub-Distributor:
            # See contracts originated by retailers under their downline
            from accounts.models import User
            # Find all retailers who have this user in their parent hierarchy
            def get_all_subordinates(parent_user):
                subordinates = list(parent_user.children.all())
                for sub in subordinates:
                    subordinates.extend(get_all_subordinates(sub))
                return subordinates

            retailer_ids = [u.id for u in get_all_subordinates(user) if u.role == 'RETAILER']
            return CustomerEMIContract.objects.filter(retailer_id__in=retailer_ids)

    def perform_create(self, serializer):
        # Default retailer to logged in user if user is Retailer
        if self.request.user.role == 'RETAILER':
            serializer.save(retailer=self.request.user)
        else:
            # If Super Admin, let them pass or default to self
            retailer = serializer.validated_data.get('retailer', self.request.user)
            serializer.save(retailer=retailer)

    @action(detail=True, methods=['get'])
    def schedule(self, request, pk=None):
        """
        Returns full EMI Schedule for Screen A-21:
        Summary Header (Total, Paid, Remaining, Installments count) + Installment rows.
        """
        contract = self.get_object()
        installments = contract.installments.all().order_by('installment_number')
        
        # Check and update overdue status dynamically
        today = timezone.localdate()
        for inst in installments:
            if inst.status in [InstallmentStatus.UPCOMING, InstallmentStatus.DUE]:
                if today > inst.due_date:
                    inst.status = InstallmentStatus.OVERDUE
                    inst.save(update_fields=['status'])

        serializer = EMIInstallmentSerializer(installments, many=True)
        return Response({
            "contract_id": contract.id,
            "customer_name": f"{contract.customer.first_name} {contract.customer.last_name}".strip() or contract.customer.username,
            "device_name": contract.device_name,
            "device_imei": contract.device_imei,
            "contract_status": contract.status,
            "summary": {
                "total_amount": contract.total_amount,
                "financed_amount": contract.financed_amount,
                "total_paid": contract.total_paid,
                "total_remaining": contract.total_remaining,
                "paid_installments_count": contract.paid_installments_count,
                "remaining_installments_count": contract.remaining_installments_count,
            },
            "installments": serializer.data
        })

    @action(detail=True, methods=['post'])
    def lock(self, request, pk=None):
        """
        Manual device lock button.
        Accessible by all users EXCEPT Customers (Super Admin, Super Dist, Dist, Sub Dist, Retailer).
        """
        if request.user.role == 'CUSTOMER':
            return Response(
                {"detail": "Customers do not have permission to lock devices."},
                status=status.HTTP_403_FORBIDDEN
            )

        contract = self.get_object()
        contract.status = EMIContractStatus.LOCKED
        contract.save(update_fields=['status'])

        from accounts.models import AuditLog
        from accounts.views import get_client_ip
        AuditLog.objects.create(
            user=request.user,
            action="MANUAL_LOCK_DEVICE",
            ip_address=get_client_ip(request),
            details={
                "contract_id": contract.id,
                "device_imei": contract.device_imei,
                "locked_by": request.user.username,
                "role": request.user.role
            }
        )

        return Response({
            "message": f"Device {contract.device_imei} has been locked manually by {request.user.role}.",
            "device_imei": contract.device_imei,
            "status": contract.status
        })

    @action(detail=True, methods=['post'])
    def unlock(self, request, pk=None):
        """
        Manual device unlock button.
        Accessible by all users EXCEPT Customers (Super Admin, Super Dist, Dist, Sub Dist, Retailer).
        """
        if request.user.role == 'CUSTOMER':
            return Response(
                {"detail": "Customers do not have permission to unlock devices."},
                status=status.HTTP_403_FORBIDDEN
            )

        contract = self.get_object()
        contract.status = EMIContractStatus.ACTIVE
        contract.save(update_fields=['status'])

        from accounts.models import AuditLog
        from accounts.views import get_client_ip
        AuditLog.objects.create(
            user=request.user,
            action="MANUAL_UNLOCK_DEVICE",
            ip_address=get_client_ip(request),
            details={
                "contract_id": contract.id,
                "device_imei": contract.device_imei,
                "unlocked_by": request.user.username,
                "role": request.user.role
            }
        )

        return Response({
            "message": f"Device {contract.device_imei} has been unlocked manually by {request.user.role}.",
            "device_imei": contract.device_imei,
            "status": contract.status
        })


class EMIInstallmentViewSet(viewsets.GenericViewSet):
    """
    Handles payment recording on individual installments (Screen A-21 Record Payment).
    """
    permission_classes = [permissions.IsAuthenticated]
    queryset = EMIInstallment.objects.all()
    serializer_class = EMIInstallmentSerializer

    @action(detail=True, methods=['post'], url_path='record-payment')
    def record_payment(self, request, pk=None):
        installment = self.get_object()
        contract = installment.contract

        # Permission check: Retailer of this contract or Super Admin
        if request.user.role not in ['SUPER_ADMIN', 'RETAILER']:
            return Response({"detail": "Only Retailers or Super Admin can record payments."}, status=status.HTTP_403_FORBIDDEN)

        if installment.status == InstallmentStatus.PAID:
            return Response({"detail": "This installment has already been paid."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = RecordPaymentSerializer(data=request.data)
        if serializer.is_valid():
            amount = serializer.validated_data['amount']
            payment_mode = serializer.validated_data['payment_mode']
            transaction_ref = serializer.validated_data.get('transaction_ref', '')
            notes = serializer.validated_data.get('notes', '')

            with transaction.atomic():
                installment.paid_amount = amount
                installment.status = InstallmentStatus.PAID
                installment.collected_by = request.user
                installment.payment_mode = payment_mode
                installment.collection_date = timezone.now()
                installment.transaction_ref = transaction_ref
                installment.notes = notes
                installment.save()

                # Check if all installments are now paid
                unpaid_count = contract.installments.exclude(status=InstallmentStatus.PAID).count()
                if unpaid_count == 0:
                    contract.status = EMIContractStatus.COMPLETED
                    contract.save(update_fields=['status'])
                elif contract.status == EMIContractStatus.LOCKED:
                    # Auto-unlock if no overdue installments remain
                    overdue_count = contract.installments.filter(status=InstallmentStatus.OVERDUE).count()
                    if overdue_count == 0:
                        contract.status = EMIContractStatus.ACTIVE
                        contract.save(update_fields=['status'])

            return Response({
                "message": f"Payment of ₹{amount} successfully recorded for Installment #{installment.installment_number}.",
                "contract_status": contract.status,
                "installment_status": installment.status
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class OverdueEMIsViewSet(viewsets.ViewSet):
    """
    KPI Cards and Overdue Customers list (Screen A-22).
    """
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        user = request.user
        today = timezone.localdate()

        # Update overdue flags on past-due installments
        EMIInstallment.objects.filter(
            status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE],
            due_date__lt=today
        ).update(status=InstallmentStatus.OVERDUE)

        # Base queryset of overdue installments
        overdue_qs = EMIInstallment.objects.filter(status=InstallmentStatus.OVERDUE).select_related('contract', 'contract__customer', 'contract__retailer')

        # Filter by hierarchy
        if user.role == 'RETAILER':
            overdue_qs = overdue_qs.filter(contract__retailer=user)
        elif user.role != 'SUPER_ADMIN':
            from accounts.models import User
            def get_all_subordinates(parent_user):
                subs = list(parent_user.children.all())
                for s in subs:
                    subs.extend(get_all_subordinates(s))
                return subs
            retailer_ids = [u.id for u in get_all_subordinates(user) if u.role == 'RETAILER']
            overdue_qs = overdue_qs.filter(contract__retailer_id__in=retailer_ids)

        # Calculate Ageing Buckets for Top KPI Cards (Screen A-22)
        total_overdue = 0
        bucket_1_7 = 0
        bucket_8_15 = 0
        bucket_16_30 = 0
        bucket_30_plus = 0

        table_rows = []
        for inst in overdue_qs:
            days = (today - inst.due_date).days
            total_overdue += 1
            if 1 <= days <= 7:
                bucket_1_7 += 1
            elif 8 <= days <= 15:
                bucket_8_15 += 1
            elif 16 <= days <= 30:
                bucket_16_30 += 1
            elif days > 30:
                bucket_30_plus += 1

            c = inst.contract.customer
            r = inst.contract.retailer
            customer_name = f"{c.first_name} {c.last_name}".strip() or c.username
            retailer_name = f"{r.first_name} {r.last_name}".strip() or r.username

            table_rows.append({
                "contract_id": inst.contract.id,
                "installment_id": inst.id,
                "customer_name": customer_name,
                "customer_phone": c.mobile_number,
                "device_name": inst.contract.device_name or "Mobile Device",
                "device_imei": inst.contract.device_imei,
                "device_status": inst.contract.status,
                "retailer_name": retailer_name,
                "retailer_store": r.business_name,
                "due_date": inst.due_date,
                "overdue_amount": inst.amount,
                "days_overdue": days,
            })

        # Check for optional bucket query filter (e.g. ?bucket=1-7 or ?bucket=8-15)
        bucket_filter = request.query_params.get('bucket')
        if bucket_filter == '1-7':
            table_rows = [r for r in table_rows if 1 <= r['days_overdue'] <= 7]
        elif bucket_filter == '8-15':
            table_rows = [r for r in table_rows if 8 <= r['days_overdue'] <= 15]
        elif bucket_filter == '16-30':
            table_rows = [r for r in table_rows if 16 <= r['days_overdue'] <= 30]
        elif bucket_filter == '30+':
            table_rows = [r for r in table_rows if r['days_overdue'] > 30]

        return Response({
            "kpis": {
                "total_overdue": total_overdue,
                "bucket_1_7_days": bucket_1_7,
                "bucket_8_15_days": bucket_8_15,
                "bucket_16_30_days": bucket_16_30,
                "bucket_30_plus_days": bucket_30_plus
            },
            "overdue_customers": table_rows
        })

import json
import logging
from decimal import Decimal
from datetime import timedelta
from django.db import transaction, models
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response

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
    VerifyOnlinePaymentSerializer,
    OverdueCustomerItemSerializer,
    PaymentItemSerializer,
    PaymentReceiptSerializer
)
from .services.razorpay_service import RazorpayService

logger = logging.getLogger(__name__)


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

    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if user.role == 'SUPER_ADMIN':
            qs = CustomerEMIContract.objects.all()
        elif user.role == 'RETAILER':
            qs = CustomerEMIContract.objects.filter(retailer=user)
        elif user.role == 'CUSTOMER':
            qs = CustomerEMIContract.objects.filter(customer=user)
        else:
            subordinates = user.get_all_subordinates()
            retailer_ids = [u.id for u in subordinates if u.role == 'RETAILER']
            qs = CustomerEMIContract.objects.filter(retailer_id__in=retailer_ids)

        # Filters from query params (Item 4 from audit doc)
        customer_param = self.request.query_params.get('customer')
        if customer_param:
            if customer_param.isdigit():
                qs = qs.filter(customer_id=customer_param)
            else:
                qs = qs.filter(
                    models.Q(customer__username__icontains=customer_param) |
                    models.Q(customer__first_name__icontains=customer_param) |
                    models.Q(customer__mobile_number__icontains=customer_param)
                )

        retailer_param = self.request.query_params.get('retailer')
        if retailer_param:
            if retailer_param.isdigit():
                qs = qs.filter(retailer_id=retailer_param)
            else:
                qs = qs.filter(
                    models.Q(retailer__username__icontains=retailer_param) |
                    models.Q(retailer__business_name__icontains=retailer_param)
                )

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param.upper())

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                models.Q(device_imei__icontains=search) |
                models.Q(device_name__icontains=search) |
                models.Q(customer__username__icontains=search) |
                models.Q(customer__first_name__icontains=search) |
                models.Q(customer__mobile_number__icontains=search) |
                models.Q(retailer__business_name__icontains=search)
            )

        ordering_param = self.request.query_params.get('ordering')
        if ordering_param:
            clean_field = ordering_param.lstrip('-')
            valid_fields = {f.name for f in CustomerEMIContract._meta.get_fields()}
            if clean_field in valid_fields:
                qs = qs.order_by(ordering_param)
            else:
                qs = qs.order_by('-created_at', '-id')
        else:
            qs = qs.order_by('-created_at', '-id')

        return qs

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

        reason = request.data.get('reason', '').strip()
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
                "role": request.user.role,
                "reason": reason
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

        reason = request.data.get('reason', '').strip()
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
                "role": request.user.role,
                "reason": reason
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

                from accounts.models import AuditLog
                from accounts.views import get_client_ip
                AuditLog.objects.create(
                    user=request.user,
                    action="RECORDED_OFFLINE_PAYMENT",
                    ip_address=get_client_ip(request),
                    details={
                        "installment_id": installment.id,
                        "contract_id": contract.id,
                        "amount": str(amount),
                        "payment_mode": payment_mode,
                        "transaction_ref": transaction_ref
                    }
                )

            return Response({
                "message": f"Payment of ₹{amount} successfully recorded for Installment #{installment.installment_number}.",
                "contract_status": contract.status,
                "installment_status": installment.status
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='create-payment-order')
    def create_payment_order(self, request, pk=None):
        """
        Creates a Razorpay Order for online customer payment (Mobile app / Web checkout).
        Returns order_id, amount in paise, currency, and public key_id.
        Operates in Simulation Mode if live gateway credentials are not yet configured.
        """
        installment = self.get_object()
        contract = installment.contract

        # Permission check: Customer of this contract, Retailer, Super Admin, or upline
        if request.user.role == 'CUSTOMER' and contract.customer != request.user:
            return Response({"detail": "You do not have permission to pay for this contract."}, status=status.HTTP_403_FORBIDDEN)

        if installment.status == InstallmentStatus.PAID:
            return Response({"detail": "This installment has already been paid."}, status=status.HTTP_400_BAD_REQUEST)

        order_data = RazorpayService.create_order(installment)
        if "error" in order_data:
            return Response(order_data, status=status.HTTP_400_BAD_REQUEST)

        return Response(order_data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='verify-online-payment')
    def verify_online_payment(self, request, pk=None):
        """
        Verifies the Razorpay checkout response signature and marks the installment as PAID.
        """
        installment = self.get_object()
        contract = installment.contract

        if request.user.role == 'CUSTOMER' and contract.customer != request.user:
            return Response({"detail": "You do not have permission to pay for this contract."}, status=status.HTTP_403_FORBIDDEN)

        if installment.status == InstallmentStatus.PAID:
            return Response({"detail": "This installment has already been paid."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = VerifyOnlinePaymentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        order_id = serializer.validated_data['razorpay_order_id']
        payment_id = serializer.validated_data['razorpay_payment_id']
        signature = serializer.validated_data['razorpay_signature']

        is_valid = RazorpayService.verify_signature(order_id, payment_id, signature)
        if not is_valid:
            return Response({"detail": "Invalid payment signature verification failed."}, status=status.HTTP_400_BAD_REQUEST)

        RazorpayService.mark_installment_paid(
            installment=installment,
            amount=installment.amount,
            payment_id=payment_id,
            notes=f"Online Payment Verified via Checkout ({payment_id})",
            user=request.user
        )

        contract.refresh_from_db()
        installment.refresh_from_db()

        return Response({
            "message": f"Payment of ₹{installment.paid_amount} successfully verified and recorded for Installment #{installment.installment_number}.",
            "payment_id": payment_id,
            "contract_status": contract.status,
            "installment_status": installment.status
        }, status=status.HTTP_200_OK)


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


class RazorpayWebhookView(APIView):
    """
    Webhook handler for asynchronous Razorpay events (e.g. payment.captured, order.paid).
    Allows external Razorpay servers to notify our backend when an online payment is captured.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        signature = request.headers.get('X-Razorpay-Signature', '')
        raw_body = request.body

        # Verify signature
        if not RazorpayService.verify_webhook_signature(raw_body, signature):
            logger.warning("Razorpay Webhook: Invalid Signature rejected.")
            return Response({"detail": "Invalid webhook signature"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as e:
            logger.error(f"Razorpay Webhook JSON decode error: {e}")
            return Response({"detail": "Invalid JSON body"}, status=status.HTTP_400_BAD_REQUEST)

        event = payload.get('event')
        logger.info(f"Razorpay Webhook received event: {event}")

        if event in ['payment.captured', 'order.paid']:
            entity = {}
            if event == 'payment.captured':
                entity = payload.get('payload', {}).get('payment', {}).get('entity', {})
                payment_id = entity.get('id')
                amount_paise = entity.get('amount', 0)
            else:
                entity = payload.get('payload', {}).get('order', {}).get('entity', {})
                payment_id = entity.get('id')
                amount_paise = entity.get('amount_paid', 0)

            notes = entity.get('notes', {})
            installment_id = notes.get('installment_id')

            if installment_id:
                try:
                    installment = EMIInstallment.objects.get(id=installment_id)
                    if installment.status != InstallmentStatus.PAID:
                        amount = Decimal(str(amount_paise)) / Decimal('100')
                        RazorpayService.mark_installment_paid(
                            installment=installment,
                            amount=amount,
                            payment_id=payment_id or f"wh_{timezone.now().timestamp()}",
                            notes=f"Razorpay Webhook Event: {event} ({payment_id})"
                        )
                        logger.info(f"Razorpay Webhook: Installment #{installment_id} marked as PAID.")
                except EMIInstallment.DoesNotExist:
                    logger.warning(f"Razorpay Webhook: Installment #{installment_id} not found.")

        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    P1 Endpoint: Screen A-23 Payments Screen & Screen A-25 Payment Receipt.
    Supports list with filters (date, mode, retailer, search) and ?export=csv.
    Supports retrieve for formatted receipt.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentItemSerializer

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return PaymentReceiptSerializer
        return PaymentItemSerializer

    def get_queryset(self):
        user = self.request.user
        qs = EMIInstallment.objects.filter(status=InstallmentStatus.PAID).select_related(
            'contract', 'contract__customer', 'contract__retailer', 'collected_by'
        )

        # Hierarchy scoping
        if user.role == 'SUPER_ADMIN':
            pass
        elif user.role == 'RETAILER':
            qs = qs.filter(contract__retailer=user)
        elif user.role == 'CUSTOMER':
            qs = qs.filter(contract__customer=user)
        else:
            subordinates = user.get_all_subordinates()
            retailer_ids = [u.id for u in subordinates if u.role == 'RETAILER']
            qs = qs.filter(contract__retailer_id__in=retailer_ids)

        # Filters
        date_param = self.request.query_params.get('date')
        if date_param:
            qs = qs.filter(collection_date__date=date_param)

        start_date = self.request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(collection_date__date__gte=start_date)

        end_date = self.request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(collection_date__date__lte=end_date)

        mode_param = self.request.query_params.get('mode')
        if mode_param:
            qs = qs.filter(payment_mode__iexact=mode_param)

        retailer_param = self.request.query_params.get('retailer')
        if retailer_param:
            if retailer_param.isdigit():
                qs = qs.filter(contract__retailer_id=retailer_param)
            else:
                qs = qs.filter(
                    models.Q(contract__retailer__username__icontains=retailer_param) |
                    models.Q(contract__retailer__business_name__icontains=retailer_param)
                )

        search = self.request.query_params.get('search')
        if search:
            rec_id = None
            if search.upper().startswith('REC-'):
                digits = search.upper().replace('REC-', '').lstrip('0')
                if digits.isdigit():
                    rec_id = int(digits)

            search_filter = (
                models.Q(transaction_ref__icontains=search) |
                models.Q(contract__device_imei__icontains=search) |
                models.Q(contract__customer__username__icontains=search) |
                models.Q(contract__customer__first_name__icontains=search) |
                models.Q(contract__customer__mobile_number__icontains=search) |
                models.Q(contract__retailer__business_name__icontains=search)
            )
            if rec_id:
                search_filter |= models.Q(id=rec_id)
            qs = qs.filter(search_filter)

        ordering_param = self.request.query_params.get('ordering')
        if ordering_param:
            alias_map = {
                'created_at': 'collection_date',
                '-created_at': '-collection_date',
                'date': 'collection_date',
                '-date': '-collection_date',
            }
            target_field = alias_map.get(ordering_param, ordering_param)
            clean_field = target_field.lstrip('-')
            valid_fields = {f.name for f in EMIInstallment._meta.get_fields()}
            if clean_field in valid_fields:
                qs = qs.order_by(target_field)
            else:
                qs = qs.order_by('-collection_date', '-id')
        else:
            qs = qs.order_by('-collection_date', '-id')

        return qs

    def list(self, request, *args, **kwargs):
        if request.query_params.get('export') == 'csv':
            from reports.utils import generate_csv_response
            qs = self.filter_queryset(self.get_queryset())
            headers = [
                "Receipt No", "Date", "Customer Name", "Customer Phone",
                "Device IMEI", "Device Name", "Retailer Store", "Amount Paid",
                "Payment Mode", "Transaction Ref", "Collected By"
            ]
            rows = []
            for item in qs[:5000]:
                c = item.contract.customer
                r = item.contract.retailer
                c_name = f"{c.first_name} {c.last_name}".strip() or c.username
                r_name = r.business_name or f"{r.first_name} {r.last_name}".strip() or r.username
                col_name = f"{item.collected_by.first_name} {item.collected_by.last_name}".strip() if item.collected_by else "System"
                date_str = item.collection_date.strftime('%Y-%m-%d %H:%M') if item.collection_date else "N/A"
                rows.append([
                    f"REC-{item.id:06d}",
                    date_str,
                    c_name,
                    c.mobile_number or "N/A",
                    item.contract.device_imei,
                    item.contract.device_name or "Phone",
                    r_name,
                    str(item.paid_amount),
                    item.payment_mode or "CASH",
                    item.transaction_ref or "N/A",
                    col_name
                ])
            return generate_csv_response("payments_report.csv", headers, rows)

        return super().list(request, *args, **kwargs)


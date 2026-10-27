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
from .services.cashfree_service import CashfreeService

from accounts.models import is_company_user

logger = logging.getLogger(__name__)


class IsSuperAdminOrReadOnly(permissions.BasePermission):
    """
    Super Admin or Staff Operations can perform any action.
    Other authenticated users have read-only access.
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.role in ['SUPER_ADMIN', 'STAFF_OPERATIONS']


class EMIPlanTemplateViewSet(viewsets.ModelViewSet):
    """
    CRUD for Master EMI Plans (Screen A-19).
    Only Super Admin or Staff Operations can create, edit, deactivate plans.
    Retailers/Distributors can view active plans.
    """
    permission_classes = [IsSuperAdminOrReadOnly]
    serializer_class = EMIPlanTemplateSerializer

    def get_queryset(self):
        user = self.request.user
        if is_company_user(user):
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
        if request.user.role not in ['SUPER_ADMIN', 'STAFF_OPERATIONS']:
            return Response(
                {"detail": "Only Super Admin or Operations Staff can activate or deactivate EMI plans."},
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
        if is_company_user(user):
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
        Accessible by all authorized users except Customers.
        """
        if request.user.role == 'CUSTOMER':
            return Response(
                {"detail": "Customers do not have permission to lock devices."},
                status=status.HTTP_403_FORBIDDEN
            )
        if request.user.role == 'STAFF_VIEWER':
            return Response(
                {"detail": "Viewer staff accounts have read-only access."},
                status=status.HTTP_403_FORBIDDEN
            )

        contract = self.get_object()
        contract.status = EMIContractStatus.LOCKED
        contract.save(update_fields=['status'])

        reason = request.data.get('reason', '').strip()

        from devices.models import Device, DeviceLockStatus, DeviceCommand, CommandType, CommandStatus, LockProvider
        from devices.services.rocketpay_service import RocketPayService
        from accounts.views import send_system_notification
        from accounts.models import NotificationType, AuditLog
        from accounts.views import get_client_ip

        device = contract.target_lock_device or Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
        if device:
            device.status = DeviceLockStatus.LOCKED
            device.save(update_fields=['status'])
            DeviceCommand.objects.create(
                device=device,
                command_type=CommandType.LOCK,
                status=CommandStatus.PENDING,
                issued_by=request.user,
                parameters={"reason": reason or "Manual lock from EMI contract"}
            )
            if device.lock_provider == LockProvider.ROCKETPAY:
                RocketPayService.lock_device(
                    device=device,
                    reason=reason or "Manual lock from EMI contract",
                    user=request.user
                )

            send_system_notification(
                recipient=contract.retailer,
                title="Device Locked",
                message=f"Device {device.imei} ({contract.customer.username}) was locked.",
                notification_type=NotificationType.DEVICE_LOCK,
                reference_id=device.id,
                sender=request.user
            )
            send_system_notification(
                recipient=contract.customer,
                title="Device Locked",
                message=f"Your device ({contract.device_imei}) has been locked.",
                notification_type=NotificationType.DEVICE_LOCK,
                reference_id=device.id,
                sender=request.user
            )

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
        Accessible by all users EXCEPT Customers.
        """
        if request.user.role == 'CUSTOMER':
            return Response(
                {"detail": "Customers do not have permission to unlock devices."},
                status=status.HTTP_403_FORBIDDEN
            )
        if request.user.role == 'STAFF_VIEWER':
            return Response(
                {"detail": "Viewer staff accounts have read-only access."},
                status=status.HTTP_403_FORBIDDEN
            )

        contract = self.get_object()
        contract.status = EMIContractStatus.ACTIVE
        contract.save(update_fields=['status'])

        reason = request.data.get('reason', '').strip()

        from devices.models import Device, DeviceLockStatus, DeviceCommand, CommandType, CommandStatus, LockProvider
        from devices.services.rocketpay_service import RocketPayService
        from accounts.views import send_system_notification
        from accounts.models import NotificationType, AuditLog
        from accounts.views import get_client_ip

        device = contract.target_lock_device or Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
        if device:
            device.status = DeviceLockStatus.ACTIVE
            device.save(update_fields=['status'])
            DeviceCommand.objects.create(
                device=device,
                command_type=CommandType.UNLOCK,
                status=CommandStatus.PENDING,
                issued_by=request.user,
                parameters={"reason": reason or "Manual unlock from EMI contract"}
            )
            if device.lock_provider == LockProvider.ROCKETPAY:
                RocketPayService.unlock_device(
                    device=device,
                    reason=reason or "Manual unlock from EMI contract",
                    user=request.user
                )

            send_system_notification(
                recipient=contract.retailer,
                title="Device Unlocked",
                message=f"Device {device.imei} was unlocked.",
                notification_type=NotificationType.DEVICE_UNLOCK,
                reference_id=device.id,
                sender=request.user
            )
            send_system_notification(
                recipient=contract.customer,
                title="Device Unlocked",
                message=f"Your device ({contract.device_imei}) has been unlocked.",
                notification_type=NotificationType.DEVICE_UNLOCK,
                reference_id=device.id,
                sender=request.user
            )

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

    @action(detail=True, methods=['post'], url_path='swap-lock-device')
    def swap_lock_device(self, request, pk=None):
        """
        TV EMI Collateral Management (Item 13):
        Swaps the customer collateral phone securing this contract.
        Refuses iPhones as collateral.
        Allows one phone to secure multiple contracts.
        """
        if request.user.role == 'CUSTOMER':
            return Response({"detail": "Customers cannot swap collateral devices."}, status=status.HTTP_403_FORBIDDEN)
        if request.user.role == 'STAFF_VIEWER':
            return Response({"detail": "Viewer staff have read-only access."}, status=status.HTTP_403_FORBIDDEN)

        contract = self.get_object()
        new_imei = request.data.get('imei') or request.data.get('device_imei')
        new_device_id = request.data.get('device_id') or request.data.get('new_device_id')

        from devices.models import Device, OSType
        target_device = None
        if new_device_id:
            target_device = Device.objects.filter(id=new_device_id).first()
        elif new_imei:
            target_device = Device.objects.filter(imei=new_imei).first()

        if not target_device:
            return Response(
                {"error": "Target collateral device not found. Please provide a valid device_id or imei."},
                status=status.HTTP_404_NOT_FOUND
            )

        # Refuse iPhones as collateral
        if target_device.os_type == OSType.IOS or (target_device.brand and 'apple' in target_device.brand.lower()):
            return Response(
                {"error": "iPhones cannot be used as collateral for TV/Appliance EMI contracts. Only Android devices are supported."},
                status=status.HTTP_400_BAD_REQUEST
            )

        contract.lock_device = target_device
        contract.save(update_fields=['lock_device'])

        from accounts.models import AuditLog
        from accounts.views import get_client_ip
        AuditLog.objects.create(
            user=request.user,
            action="SWAPPED_COLLATERAL_DEVICE",
            ip_address=get_client_ip(request),
            details={
                "contract_id": contract.id,
                "new_lock_device_id": target_device.id,
                "new_lock_device_imei": target_device.imei
            }
        )

        return Response({
            "success": True,
            "message": f"Collateral device swapped to {target_device.brand} {target_device.model} ({target_device.imei}).",
            "contract_id": contract.id,
            "lock_device_id": target_device.id,
            "lock_device_imei": target_device.imei,
            "product_type": contract.product_type
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

        # Permission check: Retailer of this contract, Super Admin, or Staff Operations / Collections
        allowed_roles = ['SUPER_ADMIN', 'RETAILER', 'STAFF_OPERATIONS', 'STAFF_COLLECTIONS']
        if request.user.role not in allowed_roles:
            return Response({"detail": "You do not have permission to record payments."}, status=status.HTTP_403_FORBIDDEN)

        if installment.status == InstallmentStatus.PAID:
            return Response({"detail": "This installment has already been paid."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = RecordPaymentSerializer(data=request.data)
        if serializer.is_valid():
            amount = serializer.validated_data['amount']
            payment_mode = serializer.validated_data['payment_mode']
            transaction_ref = serializer.validated_data.get('transaction_ref', '')
            collection_date = serializer.validated_data.get('collection_date') or timezone.now()
            notes = serializer.validated_data.get('notes', '')

            with transaction.atomic():
                installment.paid_amount = amount
                installment.status = InstallmentStatus.PAID
                installment.collected_by = request.user
                installment.payment_mode = payment_mode
                installment.collection_date = collection_date
                installment.transaction_ref = transaction_ref
                installment.notes = notes
                installment.save()

                from devices.models import Device, DeviceLockStatus, DeviceCommand, CommandType, CommandStatus, LockProvider
                from devices.services.rocketpay_service import RocketPayService
                from accounts.views import send_system_notification
                from accounts.models import NotificationType

                # Check if all installments are now paid
                unpaid_count = contract.installments.exclude(status=InstallmentStatus.PAID).count()
                overdue_count = contract.installments.filter(status=InstallmentStatus.OVERDUE).count()

                if unpaid_count == 0:
                    contract.status = EMIContractStatus.COMPLETED
                    contract.save(update_fields=['status'])

                    # RP10: If all contracts secured by this lock_device are COMPLETED, unregister RocketPay key (RBI requirement)
                    target_dev = contract.target_lock_device or Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
                    if target_dev and target_dev.lock_provider == LockProvider.ROCKETPAY:
                        other_active = CustomerEMIContract.objects.filter(
                            models.Q(lock_device=target_dev) | models.Q(device_imei=target_dev.imei)
                        ).exclude(id=contract.id).exclude(status=EMIContractStatus.COMPLETED).exists()
                        if not other_active:
                            target_dev_id = target_dev.id
                            def _trigger_on_commit_unregister():
                                from devices.models import Device
                                from devices.services.rocketpay_service import RocketPayService
                                d = Device.objects.filter(id=target_dev_id).first()
                                if d and d.provider_device_id:
                                    RocketPayService.unregister_key(d)
                            transaction.on_commit(_trigger_on_commit_unregister)
                elif overdue_count == 0 and contract.status == EMIContractStatus.LOCKED:
                    contract.status = EMIContractStatus.ACTIVE
                    contract.save(update_fields=['status'])

                # Bug B1 & Section B & Item 6: When no overdue installments remain, restore device to ACTIVE
                # and trigger RocketPay unlock via transaction.on_commit (prevents 10s timeout from holding payment transaction)
                if overdue_count == 0:
                    device = contract.target_lock_device or Device.objects.filter(emi_contract=contract).first() or Device.objects.filter(imei=contract.device_imei).first()
                    if device and device.status == DeviceLockStatus.LOCKED:
                        device.status = DeviceLockStatus.ACTIVE
                        device.save(update_fields=['status'])

                        DeviceCommand.objects.create(
                            device=device,
                            command_type=CommandType.UNLOCK,
                            status=CommandStatus.PENDING,
                            issued_by=request.user,
                            parameters={"reason": "EMI payment received - auto unlock"}
                        )

                        device_id = device.id
                        retailer_id = contract.retailer_id
                        customer_id = contract.customer_id
                        user_id = request.user.id

                        def _trigger_on_commit_unlock():
                            from devices.models import Device, LockProvider
                            from devices.services.rocketpay_service import RocketPayService
                            from accounts.views import send_system_notification
                            from accounts.models import NotificationType, User
                            dev = Device.objects.filter(id=device_id).first()
                            if not dev:
                                return
                            u = User.objects.filter(id=user_id).first()
                            if dev.lock_provider == LockProvider.ROCKETPAY and dev.provider_status != "UNREGISTERED":
                                RocketPayService.unlock_device(
                                    device=dev,
                                    reason="EMI payment received - auto unlock",
                                    user=u
                                )
                            r = User.objects.filter(id=retailer_id).first()
                            c = User.objects.filter(id=customer_id).first()
                            if r:
                                send_system_notification(
                                    recipient=r,
                                    title="Device Auto-Unlocked",
                                    message=f"Device {dev.imei} was auto-unlocked after payment receipt.",
                                    notification_type=NotificationType.DEVICE_UNLOCK,
                                    reference_id=dev.id,
                                    sender=u
                                )
                            if c:
                                send_system_notification(
                                    recipient=c,
                                    title="Device Unlocked",
                                    message=f"Your device ({dev.imei}) has been unlocked. Thank you for your payment!",
                                    notification_type=NotificationType.DEVICE_UNLOCK,
                                    reference_id=dev.id,
                                    sender=u
                                )

                        transaction.on_commit(_trigger_on_commit_unlock)

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

                # Trigger WhatsApp confirmation to customer
                try:
                    from accounts.whatsapp import WhatsAppService
                    if contract.customer and contract.customer.mobile_number:
                        WhatsAppService.send_template(
                            phone_number=contract.customer.mobile_number,
                            template_name='payment_receipt_success',
                            parameters={
                                'customer_name': contract.customer.first_name or contract.customer.username,
                                'amount': str(amount),
                                'receipt_number': f"REC-{installment.id:06d}"
                            },
                            recipient_name=contract.customer.first_name
                        )
                except Exception:
                    pass

                # Trigger in-app notification to retailer
                try:
                    from accounts.views import send_system_notification
                    if contract.retailer:
                        send_system_notification(
                            recipient=contract.retailer,
                            title="EMI Payment Recorded",
                            message=f"Payment of ₹{amount} recorded for contract #{contract.id} ({contract.device_imei}).",
                            notification_type="PAYMENT_RECEIVED",
                            reference_id=installment.id
                        )
                except Exception:
                    pass

            return Response({
                "message": f"Payment of ₹{amount} successfully recorded for Installment #{installment.installment_number}.",
                "contract_status": contract.status,
                "installment_status": installment.status
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='create-payment-order')
    def create_payment_order(self, request, pk=None):
        """
        Creates a payment order for online customer payment (Cashfree by default, Razorpay optional via ?gateway=razorpay).
        Returns Cashfree payment_session_id, order_id, order_amount for Cashfree Mobile SDK / Web checkout.
        Operates in Simulation Mode if live gateway credentials are not yet configured.
        """
        installment = self.get_object()
        contract = installment.contract

        # Permission check: Customer of this contract, Retailer, Super Admin, or upline
        if request.user.role == 'CUSTOMER' and contract.customer != request.user:
            return Response({"detail": "You do not have permission to pay for this contract."}, status=status.HTTP_403_FORBIDDEN)

        if installment.status == InstallmentStatus.PAID:
            return Response({"detail": "This installment has already been paid."}, status=status.HTTP_400_BAD_REQUEST)

        gateway = (request.query_params.get('gateway') or request.data.get('gateway', 'cashfree')).lower()
        if gateway == 'razorpay':
            order_data = RazorpayService.create_order(installment)
        else:
            return_url = request.data.get('return_url')
            order_data = CashfreeService.create_order(installment, return_url=return_url)

        if "error" in order_data:
            return Response(order_data, status=status.HTTP_400_BAD_REQUEST)

        return Response(order_data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='verify-online-payment')
    def verify_online_payment(self, request, pk=None):
        """
        Verifies online payment (Cashfree order verification or Razorpay signature) and marks installment as PAID.
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

        cf_order_id = serializer.validated_data.get('order_id') or serializer.validated_data.get('cashfree_order_id')
        if cf_order_id:
            status_res = CashfreeService.get_order_status(cf_order_id)
            if "error" in status_res:
                return Response(status_res, status=status.HTTP_400_BAD_REQUEST)
            order_status = status_res.get('order_status')
            if order_status not in ['PAID', 'SUCCESS']:
                return Response(
                    {"detail": f"Cashfree order is not paid (current status: {order_status})"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            payment_id = status_res.get('payment_id') or str(status_res.get('cf_order_id') or cf_order_id)
            paid_amt = Decimal(str(status_res.get('order_amount', installment.total_due)))
            CashfreeService.mark_installment_paid(
                installment=installment,
                amount=paid_amt,
                payment_id=payment_id,
                notes=f"Online Payment Verified via Cashfree ({payment_id})",
                user=request.user
            )
            contract.refresh_from_db()
            installment.refresh_from_db()
            return Response({
                "message": f"Payment of ₹{installment.paid_amount} successfully verified via Cashfree and recorded for Installment #{installment.installment_number}.",
                "order_id": cf_order_id,
                "payment_id": payment_id,
                "contract_status": contract.status,
                "installment_status": installment.status
            }, status=status.HTTP_200_OK)

        order_id = serializer.validated_data['razorpay_order_id']
        payment_id = serializer.validated_data['razorpay_payment_id']
        signature = serializer.validated_data['razorpay_signature']

        is_valid = RazorpayService.verify_signature(order_id, payment_id, signature)
        if not is_valid:
            return Response({"detail": "Invalid payment signature verification failed."}, status=status.HTTP_400_BAD_REQUEST)

        RazorpayService.mark_installment_paid(
            installment=installment,
            amount=installment.total_due,
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
        elif not is_company_user(user):
            retailer_ids = [u.id for u in user.get_all_subordinates() if u.role == 'RETAILER']
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
                "late_fee": inst.late_fee,
                "total_due": inst.total_due,
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


class CashfreeWebhookView(APIView):
    """
    Cashfree asynchronous webhook handler (POST /api/emi/webhook/cashfree/).
    Verifies HMAC-SHA256 signature using headers 'x-webhook-signature' and 'x-webhook-timestamp'.
    Processes PAYMENT_SUCCESS_WEBHOOK and ORDER_PAID_SUCCESS events.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        signature = request.headers.get('x-webhook-signature', '')
        timestamp = request.headers.get('x-webhook-timestamp', '')
        raw_body = request.body

        # Verify Cashfree signature
        if not CashfreeService.verify_webhook_signature(raw_body, timestamp, signature):
            logger.warning("Cashfree Webhook: Invalid Signature rejected.")
            return Response({"detail": "Invalid webhook signature"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as e:
            logger.error(f"Cashfree Webhook JSON decode error: {e}")
            return Response({"detail": "Invalid JSON body"}, status=status.HTTP_400_BAD_REQUEST)

        event_type = payload.get('type') or payload.get('event')
        logger.info(f"Cashfree Webhook received event: {event_type}")

        if event_type in ['PAYMENT_SUCCESS_WEBHOOK', 'ORDER_PAID_SUCCESS', 'PAYMENT_SUCCESS']:
            data = payload.get('data', {})
            order_info = data.get('order', {})
            payment_info = data.get('payment', {})

            order_id = order_info.get('order_id') or payload.get('order_id', '')
            cf_payment_id = payment_info.get('cf_payment_id') or order_info.get('cf_order_id') or order_id
            amount = payment_info.get('payment_amount') or order_info.get('order_amount') or 0

            installment_id = None
            if order_id.startswith('cf_inst_'):
                parts = order_id.split('_')
                if len(parts) >= 3 and parts[2].isdigit():
                    installment_id = int(parts[2])

            if installment_id:
                try:
                    installment = EMIInstallment.objects.get(id=installment_id)
                    if installment.status != InstallmentStatus.PAID:
                        paid_amt = Decimal(str(amount)) if amount else installment.total_due
                        CashfreeService.mark_installment_paid(
                            installment=installment,
                            amount=paid_amt,
                            payment_id=str(cf_payment_id),
                            notes=f"Cashfree Webhook Event: {event_type} ({cf_payment_id})"
                        )
                        logger.info(f"Cashfree Webhook: Installment #{installment_id} marked as PAID.")
                except EMIInstallment.DoesNotExist:
                    logger.warning(f"Cashfree Webhook: Installment #{installment_id} not found.")

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
        if is_company_user(user):
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


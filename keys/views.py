import uuid
import random
import string
from django.db import transaction, models
from django.contrib.auth import get_user_model
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import DeviceKey, KeyTransaction, KeyStatus, KeyAction, KeyRequest, KeyRequestStatus
from accounts.models import AuditLog
from accounts.views import get_client_ip
from .serializers import (
    DeviceKeySerializer,
    GenerateKeysSerializer,
    TransferKeysSerializer,
    ActivateKeySerializer,
    KeyRequestSerializer,
    KeyTransactionSerializer
)

User = get_user_model()

ROLE_RANK = {
    'SUPER_ADMIN': 5,
    'SUPER_DISTRIBUTOR': 4,
    'DISTRIBUTOR': 3,
    'SUB_DISTRIBUTOR': 2,
    'RETAILER': 1,
    'CUSTOMER': 0,
}

def generate_random_string(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_downstream_user(ancestor, target):
    """Check if target user is in ancestor's hierarchy"""
    curr = target.parent
    visited = set()
    while curr and curr.id not in visited:
        if curr == ancestor:
            return True
        visited.add(curr.id)
        curr = curr.parent
    return False


class KeyViewSet(viewsets.GenericViewSet, viewsets.mixins.ListModelMixin, viewsets.mixins.RetrieveModelMixin):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DeviceKeySerializer

    def get_queryset(self):
        # Users can only see keys they currently own
        return DeviceKey.objects.filter(current_owner=self.request.user)

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Only SUPER_ADMIN can generate keys"""
        if request.user.role != 'SUPER_ADMIN':
            return Response({"detail": "Only Super Admin can generate keys."}, status=status.HTTP_403_FORBIDDEN)
            
        serializer = GenerateKeysSerializer(data=request.data)
        if serializer.is_valid():
            prefix = serializer.validated_data['prefix'].upper()
            if not prefix.endswith('-'):
                prefix += '-'
                
            device_type = serializer.validated_data['device_type']
            quantity = serializer.validated_data['quantity']
            reference_note = serializer.validated_data.get('reference_note', '').strip()
            
            batch_id = uuid.uuid4()
            new_keys = []
            
            with transaction.atomic():
                for _ in range(quantity):
                    while True:
                        key_string = f"{prefix}{generate_random_string(8)}"
                        if not DeviceKey.objects.filter(key_string=key_string).exists():
                            break
                            
                    key = DeviceKey(
                        key_string=key_string,
                        device_type=device_type,
                        status=KeyStatus.AVAILABLE,
                        current_owner=request.user,
                        batch_id=batch_id
                    )
                    new_keys.append(key)
                    
                DeviceKey.objects.bulk_create(new_keys)
                
                created_keys = DeviceKey.objects.filter(batch_id=batch_id)
                transactions = [
                    KeyTransaction(
                        key=k,
                        from_user=request.user,
                        to_user=request.user,
                        action=KeyAction.GENERATED,
                        notes=reference_note or f"Generated {quantity} {device_type} keys"
                    )
                    for k in created_keys
                ]
                KeyTransaction.objects.bulk_create(transactions)
                
                AuditLog.objects.create(
                    user=request.user,
                    action="GENERATED_KEYS",
                    ip_address=get_client_ip(request),
                    details={
                        "batch_id": str(batch_id),
                        "quantity": quantity,
                        "device_type": device_type,
                        "prefix": prefix,
                        "reference_note": reference_note
                    }
                )

            return Response({
                "message": f"Successfully generated {quantity} keys.",
                "batch_id": batch_id
            }, status=status.HTTP_201_CREATED)
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """Get summary of owned keys grouped by device type and status"""
        from django.db.models import Count
        
        summary = DeviceKey.objects.filter(current_owner=request.user) \
            .values('device_type', 'status') \
            .annotate(count=Count('id')) \
            .order_by('device_type', 'status')
            
        return Response({"inventory": summary})

    @action(detail=False, methods=['get'])
    def recipients(self, request):
        """
        Step 1: Select Role -> GET /api/keys/recipients/ (returns allowed roles)
        Step 2: Select User of that Role -> GET /api/keys/recipients/?role=RETAILER (returns users of that role)
        """
        user = request.user
        sender_rank = ROLE_RANK.get(user.role, 0)

        # Lower-tier roles that this user can allocate to
        allowed_roles = [
            role for role, rank in ROLE_RANK.items()
            if rank < sender_rank and role != 'CUSTOMER'
        ]

        selected_role = request.query_params.get('role')
        if not selected_role:
            return Response({
                "selectable_roles": allowed_roles
            })

        selected_role = selected_role.upper()
        if selected_role not in allowed_roles:
            return Response({
                "detail": f"Invalid role '{selected_role}'. You can select from: {allowed_roles}"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get active users of this role
        if user.role == 'SUPER_ADMIN':
            users_qs = User.objects.filter(role=selected_role, is_active=True)
        else:
            # Users in their downstream network
            def get_all_subordinates(parent_user):
                subs = list(parent_user.children.all())
                for s in subs:
                    subs.extend(get_all_subordinates(s))
                return subs

            sub_ids = [u.id for u in get_all_subordinates(user)]
            users_qs = User.objects.filter(id__in=sub_ids, role=selected_role, is_active=True)

        device_type = request.query_params.get('device_type')

        data = []
        for u in users_qs:
            keys_qs = DeviceKey.objects.filter(current_owner=u, status=KeyStatus.AVAILABLE)
            if device_type:
                keys_qs = keys_qs.filter(device_type=device_type)
            keys_count = keys_qs.count()

            name = f"{u.first_name} {u.last_name}".strip() or u.username
            data.append({
                "id": u.id,
                "name": name,
                "username": u.username,
                "role": u.role,
                "business_name": u.business_name,
                "mobile_number": u.mobile_number,
                "city": u.city,
                "state": u.state,
                "current_available_keys": keys_count
            })

        return Response({
            "selected_role": selected_role,
            "count": len(data),
            "users": data
        })

    @action(detail=False, methods=['post'])
    def allocate(self, request):
        """
        Transfer keys down the hierarchy at any time.
        Super-Admin can transfer directly to Super Distributors, Distributors, Sub-Distributors, or Retailers.
        Super-Distributors can transfer directly to Sub-Distributors or Retailers in their network.
        """
        serializer = TransferKeysSerializer(data=request.data)
        if serializer.is_valid():
            target_role = serializer.validated_data.get('target_role')
            target_user_id = serializer.validated_data['target_user_id']
            device_type = serializer.validated_data['device_type']
            quantity = serializer.validated_data['quantity']
            reference_note = serializer.validated_data.get('reference_note', '').strip()
            
            target_user = User.objects.filter(id=target_user_id).first()
            if not target_user:
                return Response({"detail": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)
                
            # If target_role was provided, validate matching role
            if target_role and target_user.role != target_role.upper():
                return Response({
                    "detail": f"Target user {target_user.username} has role '{target_user.role}', but you selected '{target_role}'."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Cannot transfer to self
            if target_user == request.user:
                return Response({"detail": "You cannot allocate keys to yourself. Please specify a recipient downstream user."}, status=status.HTTP_400_BAD_REQUEST)

            # Cannot transfer to Customers (they don't hold inventory)
            if target_user.role == 'CUSTOMER':
                return Response({"detail": "Keys cannot be allocated to Customers. Keys are activated when registering a device for a customer."}, status=status.HTTP_400_BAD_REQUEST)

            sender_rank = ROLE_RANK.get(request.user.role, 0)
            target_rank = ROLE_RANK.get(target_user.role, 0)

            # Must transfer to a lower role tier
            if sender_rank <= target_rank:
                return Response({
                    "detail": f"You can only allocate keys to lower-tier roles. Your role is {request.user.role}, but target is {target_user.role}."
                }, status=status.HTTP_403_FORBIDDEN)
                
            target_name = f"{target_user.first_name} {target_user.last_name}".strip() or target_user.username
            with transaction.atomic():
                available_keys = DeviceKey.objects.filter(
                    current_owner=request.user,
                    device_type=device_type,
                    status=KeyStatus.AVAILABLE
                )[:quantity]
                
                if len(available_keys) < quantity:
                    return Response({
                        "detail": f"Insufficient {device_type} keys available. You currently hold {len(available_keys)} available {device_type} keys."
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                key_ids = [k.id for k in available_keys]
                
                DeviceKey.objects.filter(id__in=key_ids).update(current_owner=target_user)
                
                transactions = [
                    KeyTransaction(
                        key_id=k_id, 
                        from_user=request.user, 
                        to_user=target_user, 
                        action=KeyAction.ALLOCATED,
                        notes=reference_note or f"Allocated {quantity} {device_type} keys to {target_name}"
                    ) for k_id in key_ids
                ]
                KeyTransaction.objects.bulk_create(transactions)
                
            remaining_count = DeviceKey.objects.filter(
                current_owner=request.user,
                device_type=device_type,
                status=KeyStatus.AVAILABLE
            ).count()

            AuditLog.objects.create(
                user=request.user,
                action="ALLOCATED_KEYS",
                ip_address=get_client_ip(request),
                details={
                    "target_user": target_user.username,
                    "quantity": quantity,
                    "device_type": device_type,
                    "reference_note": reference_note
                }
            )

            return Response({
                "message": f"Successfully allocated {quantity} {device_type} keys to {target_name} ({target_user.role}).",
                "recipient": {
                    "id": target_user.id,
                    "name": target_name,
                    "role": target_user.role,
                    "business_name": target_user.business_name
                },
                "your_remaining_available_keys": remaining_count
            })
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'])
    def reclaim(self, request):
        """
        Unilateral Reclaim: Higher authorities have full access to retrieve unactivated keys
        from lower-tier users irrespective of reasons, and without requiring permission from the lower user.
        - Super-Admin can reclaim keys from any user in the network.
        - Super Distributors, Distributors, and Sub-Distributors can reclaim from their downline users.
        """
        serializer = TransferKeysSerializer(data=request.data)
        if serializer.is_valid():
            target_user_id = serializer.validated_data['target_user_id']
            device_type = serializer.validated_data['device_type']
            quantity = serializer.validated_data['quantity']
            
            target_user = User.objects.filter(id=target_user_id).first()
            if not target_user:
                return Response({"detail": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)
                
            if target_user == request.user:
                return Response({"detail": "You cannot reclaim keys from yourself."}, status=status.HTTP_400_BAD_REQUEST)

            sender_rank = ROLE_RANK.get(request.user.role, 0)
            target_rank = ROLE_RANK.get(target_user.role, 0)

            if sender_rank <= target_rank:
                return Response({
                    "detail": f"You can only reclaim keys from lower-tier roles. Your role is {request.user.role}, target is {target_user.role}."
                }, status=status.HTTP_403_FORBIDDEN)

            # Higher authorities can reclaim from their own downline; Super-Admin can reclaim from anyone
            if request.user.role != 'SUPER_ADMIN' and not is_downstream_user(request.user, target_user):
                return Response({
                    "detail": "You can only reclaim keys from downline users within your own hierarchy."
                }, status=status.HTTP_403_FORBIDDEN)
                
            target_name = f"{target_user.first_name} {target_user.last_name}".strip() or target_user.username
            reason = request.data.get('reason', '').strip()
            with transaction.atomic():
                available_keys = DeviceKey.objects.filter(
                    current_owner=target_user,
                    device_type=device_type,
                    status=KeyStatus.AVAILABLE
                )[:quantity]
                
                if len(available_keys) < quantity:
                    return Response({
                        "detail": f"Target user {target_user.username} only has {len(available_keys)} available {device_type} keys to reclaim."
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                key_ids = [k.id for k in available_keys]
                
                DeviceKey.objects.filter(id__in=key_ids).update(current_owner=request.user)
                
                transactions = [
                    KeyTransaction(
                        key_id=k_id, 
                        from_user=target_user, 
                        to_user=request.user, 
                        action=KeyAction.RECLAIMED,
                        notes=reason or f"Reclaimed {quantity} {device_type} keys from {target_name}"
                    ) for k_id in key_ids
                ]
                KeyTransaction.objects.bulk_create(transactions)

            your_total = DeviceKey.objects.filter(
                current_owner=request.user,
                device_type=device_type,
                status=KeyStatus.AVAILABLE
            ).count()

            AuditLog.objects.create(
                user=request.user,
                action="RECLAIMED_KEYS",
                ip_address=get_client_ip(request),
                details={
                    "reclaimed_from": target_user.username,
                    "quantity": quantity,
                    "device_type": device_type,
                    "reason": reason
                }
            )
                
            return Response({
                "message": f"Successfully reclaimed {quantity} {device_type} keys from {target_name} ({target_user.role}).",
                "reclaimed_from": {
                    "id": target_user.id,
                    "name": target_name,
                    "role": target_user.role
                },
                "your_total_available_keys": your_total
            })
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'])
    def activate(self, request):
        """Retailer activates a key for a customer/device"""
        serializer = ActivateKeySerializer(data=request.data)
        if serializer.is_valid():
            key_string = serializer.validated_data['key_string']
            device_id = serializer.validated_data['device_id']
            customer_id = serializer.validated_data['customer_id']
            
            customer = User.objects.filter(id=customer_id, role='CUSTOMER').first()
            if not customer:
                return Response({"detail": "Customer not found."}, status=status.HTTP_404_NOT_FOUND)
                
            with transaction.atomic():
                key = DeviceKey.objects.select_for_update().filter(key_string=key_string).first()
                if not key:
                    return Response({"detail": "Invalid key."}, status=status.HTTP_404_NOT_FOUND)
                    
                if key.current_owner != request.user:
                    return Response({"detail": "You do not own this key."}, status=status.HTTP_403_FORBIDDEN)
                    
                if key.status != KeyStatus.AVAILABLE:
                    return Response({"detail": f"Key is already {key.status}."}, status=status.HTTP_400_BAD_REQUEST)
                    
                from django.utils import timezone
                
                key.status = KeyStatus.ACTIVATED
                key.activated_on_device_id = device_id
                key.activated_for_customer = customer
                key.activated_at = timezone.now()
                key.save()
                
                KeyTransaction.objects.create(
                    key=key,
                    from_user=request.user,
                    to_user=customer,
                    action=KeyAction.ACTIVATED
                )
                
            return Response({"message": f"Key successfully activated for device {device_id}."})
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='transactions')
    def transactions(self, request):
        """
        P1 Endpoint: Key history (Screen N-07).
        Lists all key lifecycle events (GENERATED, ALLOCATED, RECLAIMED, ACTIVATED).
        Filters: ?type=, ?device_type=, ?start_date=, ?end_date=, ?search=, ?export=csv
        """
        user = request.user
        qs = KeyTransaction.objects.select_related('key', 'from_user', 'to_user').order_by('-timestamp')

        if user.role != 'SUPER_ADMIN':
            subordinates = user.get_all_subordinates()
            sub_ids = [u.id for u in subordinates]
            qs = qs.filter(
                models.Q(from_user=user) | models.Q(to_user=user) |
                models.Q(from_user_id__in=sub_ids) | models.Q(to_user_id__in=sub_ids)
            )

        action_param = request.query_params.get('type') or request.query_params.get('action')
        if action_param:
            qs = qs.filter(action__iexact=action_param)

        device_type = request.query_params.get('device_type')
        if device_type:
            qs = qs.filter(key__device_type__iexact=device_type)

        start_date = request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(timestamp__date__gte=start_date)

        end_date = request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(timestamp__date__lte=end_date)

        search = request.query_params.get('search')
        if search:
            qs = qs.filter(
                models.Q(key__key_string__icontains=search) |
                models.Q(from_user__username__icontains=search) |
                models.Q(from_user__first_name__icontains=search) |
                models.Q(from_user__business_name__icontains=search) |
                models.Q(to_user__username__icontains=search) |
                models.Q(to_user__first_name__icontains=search) |
                models.Q(to_user__business_name__icontains=search)
            )

        ordering_param = request.query_params.get('ordering')
        if ordering_param:
            alias_map = {
                'created_at': 'timestamp',
                '-created_at': '-timestamp',
                'date': 'timestamp',
                '-date': '-timestamp',
            }
            target_field = alias_map.get(ordering_param, ordering_param)
            clean_field = target_field.lstrip('-')
            valid_fields = {f.name for f in KeyTransaction._meta.get_fields()}
            if clean_field in valid_fields:
                qs = qs.order_by(target_field)
            else:
                qs = qs.order_by('-timestamp', '-id')

        if request.query_params.get('export') == 'csv':
            from reports.utils import generate_csv_response
            headers = ["ID", "Key String", "Device Type", "Action", "From User", "To User", "Date Time"]
            rows = []
            for t in qs[:5000]:
                f_name = (f"{t.from_user.first_name} {t.from_user.last_name}".strip() or t.from_user.username) if t.from_user else "System"
                t_name = (f"{t.to_user.first_name} {t.to_user.last_name}".strip() or t.to_user.username) if t.to_user else "N/A"
                rows.append([
                    t.id,
                    t.key.key_string,
                    t.key.device_type,
                    t.action,
                    f_name,
                    t_name,
                    t.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                ])
            return generate_csv_response("key_transactions_report.csv", headers, rows)

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = KeyTransactionSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = KeyTransactionSerializer(qs, many=True)
        return Response(serializer.data)


class KeyRequestViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = KeyRequestSerializer

    def get_queryset(self):
        user = self.request.user
        qs = KeyRequest.objects.filter(
            models.Q(requester=user) | models.Q(parent=user)
        )
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                models.Q(requester__username__icontains=search) |
                models.Q(requester__first_name__icontains=search) |
                models.Q(requester__business_name__icontains=search) |
                models.Q(device_type__icontains=search) |
                models.Q(os_type__icontains=search) |
                models.Q(notes__icontains=search)
            )
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        if not self.request.user.parent:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"detail": "You do not have a parent assigned to request keys from."})
        
        device_type = serializer.validated_data.get('device_type', '').upper()
        os_type = serializer.validated_data.get('os_type')
        if not os_type:
            if device_type in ['ANDROID', 'MOBILE_ANDROID']:
                os_type = 'ANDROID'
            elif device_type in ['IOS', 'IPHONE', 'MOBILE_IOS']:
                os_type = 'IOS'
            elif device_type in ['TV', 'SMART_TV']:
                os_type = 'SMART_TV'
            
        instance = serializer.save(
            requester=self.request.user,
            parent=self.request.user.parent,
            os_type=os_type,
            status=KeyRequestStatus.PENDING
        )

        AuditLog.objects.create(
            user=self.request.user,
            action="CREATED_KEY_REQUEST",
            ip_address=get_client_ip(self.request),
            details={
                "request_id": instance.id,
                "parent_id": instance.parent_id,
                "quantity": instance.quantity,
                "device_type": instance.device_type,
                "os_type": instance.os_type
            }
        )

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        key_req = self.get_object()
        
        if key_req.parent != request.user:
            return Response({"detail": "You are not authorized to approve this request."}, status=status.HTTP_403_FORBIDDEN)
            
        if key_req.status != KeyRequestStatus.PENDING:
            return Response({"detail": f"Request is already {key_req.status}."}, status=status.HTTP_400_BAD_REQUEST)

        # Support partial approval via ?quantity or body
        approve_qty = key_req.quantity
        if 'quantity' in request.data:
            try:
                custom_qty = int(request.data['quantity'])
                if custom_qty > 0:
                    approve_qty = min(custom_qty, key_req.quantity)
            except (ValueError, TypeError):
                pass
            
        with transaction.atomic():
            requested_target = (key_req.os_type or key_req.device_type or '').upper()

            # Match based on requested platform and key prefix/device_type
            keys_filter = models.Q(current_owner=request.user, status=KeyStatus.AVAILABLE)
            if requested_target in ['IOS', 'IPHONE', 'MOBILE_IOS']:
                keys_filter &= (models.Q(device_type__iexact='IOS') | models.Q(key_string__istartswith='IOS-'))
            elif requested_target in ['ANDROID', 'MOBILE_ANDROID']:
                keys_filter &= (models.Q(device_type__iexact='ANDROID') | models.Q(key_string__istartswith='AND-') | models.Q(device_type='MOBILE'))
            elif requested_target in ['TV', 'SMART_TV']:
                keys_filter &= (models.Q(device_type__iexact='TV') | models.Q(key_string__istartswith='TV-'))
            else:
                keys_filter &= models.Q(device_type__iexact=key_req.device_type)

            available_keys = DeviceKey.objects.filter(keys_filter)[:approve_qty]
            
            if len(available_keys) < approve_qty:
                return Response({
                    "detail": f"You do not have enough {requested_target} keys available to fulfill this request. Available: {len(available_keys)}, Requested: {approve_qty}."
                }, status=status.HTTP_400_BAD_REQUEST)
                
            key_ids = [k.id for k in available_keys]
            DeviceKey.objects.filter(id__in=key_ids).update(current_owner=key_req.requester)
            
            transactions = [
                KeyTransaction(
                    key_id=k_id, 
                    from_user=request.user, 
                    to_user=key_req.requester, 
                    action=KeyAction.ALLOCATED
                ) for k_id in key_ids
            ]
            KeyTransaction.objects.bulk_create(transactions)
            
            if approve_qty < key_req.quantity:
                key_req.notes = f"{key_req.notes or ''} [Partially Approved: {approve_qty}/{key_req.quantity}]".strip()
                key_req.quantity = approve_qty

            key_req.status = KeyRequestStatus.APPROVED
            key_req.save()
            
            transferred_key_strings = list(DeviceKey.objects.filter(id__in=key_ids).values_list('key_string', flat=True))

            AuditLog.objects.create(
                user=request.user,
                action="APPROVED_KEY_REQUEST",
                ip_address=get_client_ip(request),
                details={
                    "request_id": key_req.id,
                    "requester": key_req.requester.username,
                    "approved_quantity": approve_qty,
                    "transferred_keys": transferred_key_strings
                }
            )

        return Response({
            "message": f"Successfully approved {approve_qty} {requested_target} keys.",
            "transferred_keys": transferred_key_strings,
            "approved_quantity": approve_qty
        })

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        key_req = self.get_object()
        
        if key_req.parent != request.user:
            return Response({"detail": "You are not authorized to reject this request."}, status=status.HTTP_403_FORBIDDEN)
            
        if key_req.status != KeyRequestStatus.PENDING:
            return Response({"detail": f"Request is already {key_req.status}."}, status=status.HTTP_400_BAD_REQUEST)
            
        reason = request.data.get('reason', '')
        if reason:
            key_req.notes = f"{key_req.notes or ''} [Rejected Reason: {reason}]".strip()

        key_req.status = KeyRequestStatus.REJECTED
        key_req.save()

        AuditLog.objects.create(
            user=request.user,
            action="REJECTED_KEY_REQUEST",
            ip_address=get_client_ip(request),
            details={
                "request_id": key_req.id,
                "requester": key_req.requester.username,
                "quantity": key_req.quantity,
                "reason": reason
            }
        )

        return Response({"message": "Request rejected.", "reason": reason})

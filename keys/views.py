import uuid
import random
import string
from django.db import transaction, models
from django.contrib.auth import get_user_model
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import DeviceKey, KeyTransaction, KeyStatus, KeyAction, KeyRequest, KeyRequestStatus
from .serializers import (
    DeviceKeySerializer,
    GenerateKeysSerializer,
    TransferKeysSerializer,
    ActivateKeySerializer,
    KeyRequestSerializer
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
                    KeyTransaction(key=k, to_user=request.user, action=KeyAction.GENERATED)
                    for k in created_keys
                ]
                KeyTransaction.objects.bulk_create(transactions)
                
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
                        action=KeyAction.ALLOCATED
                    ) for k_id in key_ids
                ]
                KeyTransaction.objects.bulk_create(transactions)
                
            target_name = f"{target_user.first_name} {target_user.last_name}".strip() or target_user.username
            remaining_count = DeviceKey.objects.filter(
                current_owner=request.user,
                device_type=device_type,
                status=KeyStatus.AVAILABLE
            ).count()

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
            if request.user.role != 'SUPER_ADMIN' and not is_ancestor_of(request.user, target_user):
                return Response({
                    "detail": "You can only reclaim keys from downline users within your own hierarchy."
                }, status=status.HTTP_403_FORBIDDEN)
                
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
                        action=KeyAction.RECLAIMED
                    ) for k_id in key_ids
                ]
                KeyTransaction.objects.bulk_create(transactions)

            target_name = f"{target_user.first_name} {target_user.last_name}".strip() or target_user.username
            your_total = DeviceKey.objects.filter(
                current_owner=request.user,
                device_type=device_type,
                status=KeyStatus.AVAILABLE
            ).count()
                
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


class KeyRequestViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = KeyRequestSerializer

    def get_queryset(self):
        return KeyRequest.objects.filter(
            models.Q(requester=self.request.user) | models.Q(parent=self.request.user)
        ).order_by('-created_at')

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
            
        serializer.save(
            requester=self.request.user,
            parent=self.request.user.parent,
            os_type=os_type,
            status=KeyRequestStatus.PENDING
        )

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        key_req = self.get_object()
        
        if key_req.parent != request.user:
            return Response({"detail": "You are not authorized to approve this request."}, status=status.HTTP_403_FORBIDDEN)
            
        if key_req.status != KeyRequestStatus.PENDING:
            return Response({"detail": f"Request is already {key_req.status}."}, status=status.HTTP_400_BAD_REQUEST)
            
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

            available_keys = DeviceKey.objects.filter(keys_filter)[:key_req.quantity]
            
            if len(available_keys) < key_req.quantity:
                return Response({
                    "detail": f"You do not have enough {requested_target} keys available to fulfill this request. Available: {len(available_keys)}, Requested: {key_req.quantity}."
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
            
            key_req.status = KeyRequestStatus.APPROVED
            key_req.save()
            
            transferred_key_strings = list(DeviceKey.objects.filter(id__in=key_ids).values_list('key_string', flat=True))

        return Response({
            "message": f"Successfully approved {key_req.quantity} {requested_target} keys.",
            "transferred_keys": transferred_key_strings
        })

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        key_req = self.get_object()
        
        if key_req.parent != request.user:
            return Response({"detail": "You are not authorized to reject this request."}, status=status.HTTP_403_FORBIDDEN)
            
        if key_req.status != KeyRequestStatus.PENDING:
            return Response({"detail": f"Request is already {key_req.status}."}, status=status.HTTP_400_BAD_REQUEST)
            
        key_req.status = KeyRequestStatus.REJECTED
        key_req.save()
        return Response({"message": "Request rejected."})

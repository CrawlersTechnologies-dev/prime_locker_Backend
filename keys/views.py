import uuid
import random
import string
from django.db import transaction
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

def generate_random_string(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

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
            
            # Use a transaction to ensure either all keys are generated or none
            with transaction.atomic():
                for _ in range(quantity):
                    # Ensure uniqueness
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
                
                # Fetch created keys for transactions
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

    @action(detail=False, methods=['post'])
    def allocate(self, request):
        """Transfer keys down to a child node"""
        serializer = TransferKeysSerializer(data=request.data)
        if serializer.is_valid():
            target_user_id = serializer.validated_data['target_user_id']
            device_type = serializer.validated_data['device_type']
            quantity = serializer.validated_data['quantity']
            
            target_user = User.objects.filter(id=target_user_id).first()
            if not target_user:
                return Response({"detail": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)
                
            # Hierarchical check: Target must be a direct child (or we can allow any descendant)
            # For strictness, let's allow allocating to direct children only.
            if target_user.parent != request.user and request.user.role != 'SUPER_ADMIN':
                return Response({"detail": "You can only allocate keys to your direct downstream users."}, status=status.HTTP_403_FORBIDDEN)
                
            with transaction.atomic():
                available_keys = DeviceKey.objects.filter(
                    current_owner=request.user,
                    device_type=device_type,
                    status=KeyStatus.AVAILABLE
                )[:quantity]
                
                if len(available_keys) < quantity:
                    return Response({"detail": f"Insufficient {device_type} keys available."}, status=status.HTTP_400_BAD_REQUEST)
                
                key_ids = [k.id for k in available_keys]
                
                # Update ownership
                DeviceKey.objects.filter(id__in=key_ids).update(current_owner=target_user)
                
                # Log transactions
                transactions = [
                    KeyTransaction(
                        key_id=k_id, 
                        from_user=request.user, 
                        to_user=target_user, 
                        action=KeyAction.ALLOCATED
                    ) for k_id in key_ids
                ]
                KeyTransaction.objects.bulk_create(transactions)
                
            return Response({"message": f"Successfully allocated {quantity} keys to {target_user.username}."})
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'])
    def reclaim(self, request):
        """Reclaim unused keys from a child node"""
        serializer = TransferKeysSerializer(data=request.data)
        if serializer.is_valid():
            target_user_id = serializer.validated_data['target_user_id']
            device_type = serializer.validated_data['device_type']
            quantity = serializer.validated_data['quantity']
            
            target_user = User.objects.filter(id=target_user_id).first()
            if not target_user:
                return Response({"detail": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)
                
            # Hierarchical check
            if target_user.parent != request.user and request.user.role != 'SUPER_ADMIN':
                return Response({"detail": "You can only reclaim keys from your direct downstream users."}, status=status.HTTP_403_FORBIDDEN)
                
            # Check permissions
            if not request.user.can_reclaim_keys and request.user.role != 'SUPER_ADMIN':
                return Response({"detail": "You do not have permission to reclaim keys."}, status=status.HTTP_403_FORBIDDEN)
                
            with transaction.atomic():
                available_keys = DeviceKey.objects.filter(
                    current_owner=target_user,
                    device_type=device_type,
                    status=KeyStatus.AVAILABLE
                )[:quantity]
                
                if len(available_keys) < quantity:
                    return Response({"detail": f"Target user does not have {quantity} available {device_type} keys to reclaim."}, status=status.HTTP_400_BAD_REQUEST)
                
                key_ids = [k.id for k in available_keys]
                
                # Reclaim ownership
                DeviceKey.objects.filter(id__in=key_ids).update(current_owner=request.user)
                
                # Log transactions
                transactions = [
                    KeyTransaction(
                        key_id=k_id, 
                        from_user=target_user, 
                        to_user=request.user, 
                        action=KeyAction.RECLAIMED
                    ) for k_id in key_ids
                ]
                KeyTransaction.objects.bulk_create(transactions)
                
            return Response({"message": f"Successfully reclaimed {quantity} keys from {target_user.username}."})
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'])
    def activate(self, request):
        """Retailer activates a key for a customer/device"""
        # Only Retailers (and maybe SUPER_ADMIN) should activate?
        # We can leave this available to whoever owns the key
        serializer = ActivateKeySerializer(data=request.data)
        if serializer.is_valid():
            key_string = serializer.validated_data['key_string']
            device_id = serializer.validated_data['device_id']
            customer_id = serializer.validated_data['customer_id']
            
            customer = User.objects.filter(id=customer_id, role='CUSTOMER').first()
            if not customer:
                return Response({"detail": "Customer not found."}, status=status.HTTP_404_NOT_FOUND)
                
            with transaction.atomic():
                # Lock the key row
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
        # A user sees requests they made OR requests made to them
        from django.db import models
        return KeyRequest.objects.filter(
            models.Q(requester=self.request.user) | models.Q(parent=self.request.user)
        ).order_by('-created_at')

    def perform_create(self, serializer):
        # The user making the request must have a parent
        if not self.request.user.parent:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"detail": "You do not have a parent assigned to request keys from."})
            
        serializer.save(
            requester=self.request.user,
            parent=self.request.user.parent,
            status=KeyRequestStatus.PENDING
        )

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        key_req = self.get_object()
        
        # Only the parent can approve
        if key_req.parent != request.user:
            return Response({"detail": "You are not authorized to approve this request."}, status=status.HTTP_403_FORBIDDEN)
            
        if key_req.status != KeyRequestStatus.PENDING:
            return Response({"detail": f"Request is already {key_req.status}."}, status=status.HTTP_400_BAD_REQUEST)
            
        # Check if parent has enough keys
        with transaction.atomic():
            available_keys = DeviceKey.objects.filter(
                current_owner=request.user,
                device_type=key_req.device_type,
                status=KeyStatus.AVAILABLE
            )[:key_req.quantity]
            
            if len(available_keys) < key_req.quantity:
                return Response({"detail": f"You do not have enough {key_req.device_type} keys available to fulfill this request."}, status=status.HTTP_400_BAD_REQUEST)
                
            key_ids = [k.id for k in available_keys]
            DeviceKey.objects.filter(id__in=key_ids).update(current_owner=key_req.requester)
            
            # Log transactions
            transactions = [
                KeyTransaction(
                    key_id=k_id, 
                    from_user=request.user, 
                    to_user=key_req.requester, 
                    action=KeyAction.ALLOCATED
                ) for k_id in key_ids
            ]
            KeyTransaction.objects.bulk_create(transactions)
            
            # Update request status
            key_req.status = KeyRequestStatus.APPROVED
            key_req.save()
            
        return Response({"message": f"Successfully approved {key_req.quantity} keys."})

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

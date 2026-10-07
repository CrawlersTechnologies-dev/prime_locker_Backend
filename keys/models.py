from django.db import models
from django.contrib.auth import get_user_model
import uuid

User = get_user_model()

class KeyStatus(models.TextChoices):
    AVAILABLE = 'AVAILABLE', 'Available'
    ACTIVATED = 'ACTIVATED', 'Activated'
    REVOKED = 'REVOKED', 'Revoked'

class DeviceKey(models.Model):
    key_string = models.CharField(max_length=50, unique=True, db_index=True)
    device_type = models.CharField(max_length=50) # e.g., 'MOBILE', 'TV'
    status = models.CharField(max_length=20, choices=KeyStatus.choices, default=KeyStatus.AVAILABLE)
    
    # Ownership tracking
    current_owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_keys')
    batch_id = models.UUIDField(default=uuid.uuid4, editable=False) # Groups keys generated together
    
    # Activation details
    activated_on_device_id = models.CharField(max_length=100, null=True, blank=True)
    activated_for_customer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='activated_keys')
    activated_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key_string} - {self.status}"

class KeyAction(models.TextChoices):
    GENERATED = 'GENERATED', 'Generated'
    ALLOCATED = 'ALLOCATED', 'Allocated'
    RECLAIMED = 'RECLAIMED', 'Reclaimed'
    ACTIVATED = 'ACTIVATED', 'Activated'

class KeyTransaction(models.Model):
    key = models.ForeignKey(DeviceKey, on_delete=models.CASCADE, related_name='transactions')
    from_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers_out')
    to_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers_in')
    action = models.CharField(max_length=20, choices=KeyAction.choices)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.key.key_string} - {self.action} at {self.timestamp}"

class KeyRequestStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'

class KeyRequest(models.Model):
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='key_requests_made')
    parent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='key_requests_received')
    device_type = models.CharField(max_length=50)
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=KeyRequestStatus.choices, default=KeyRequestStatus.PENDING)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.quantity} {self.device_type} keys requested by {self.requester.username}"

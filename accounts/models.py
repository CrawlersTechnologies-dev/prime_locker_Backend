from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager

class Role(models.TextChoices):
    SUPER_ADMIN = 'SUPER_ADMIN', 'Super Admin'
    SUPER_DISTRIBUTOR = 'SUPER_DISTRIBUTOR', 'Super Distributor / Super Seller'
    DISTRIBUTOR = 'DISTRIBUTOR', 'Distributor'
    SUB_DISTRIBUTOR = 'SUB_DISTRIBUTOR', 'Sub Distributor'
    RETAILER = 'RETAILER', 'Retailer'
    CUSTOMER = 'CUSTOMER', 'Customer'

class UserManager(BaseUserManager):
    def create_user(self, username, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', Role.SUPER_ADMIN)
        return self.create_user(username, email, password, **extra_fields)

class User(AbstractUser):
    role = models.CharField(max_length=50, choices=Role.choices, default=Role.CUSTOMER)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    mobile_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    alternate_mobile_number = models.CharField(max_length=15, null=True, blank=True)
    
    # Common Business & Address Fields
    business_name = models.CharField(max_length=255, null=True, blank=True)
    pan_number = models.CharField(max_length=10, null=True, blank=True)
    gst_number = models.CharField(max_length=15, null=True, blank=True)
    state = models.CharField(max_length=100, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    pincode = models.CharField(max_length=10, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    
    # Documents and Settings
    kyc_document = models.FileField(upload_to='kyc_documents/', null=True, blank=True)
    customer_photo = models.FileField(upload_to='customer_photos/', null=True, blank=True)
    customer_signature = models.FileField(upload_to='customer_signatures/', null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    can_reclaim_keys = models.BooleanField(default=False)
    
    objects = UserManager()

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

class AuditLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.action} by {self.user} at {self.timestamp}"

from django.utils import timezone

class PasswordResetOTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_valid(self):
        # OTP is valid for 10 minutes (600 seconds)
        return not self.is_used and (timezone.now() - self.created_at).seconds < 600

    def __str__(self):
        return f"OTP for {self.user.username}"

from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager

class Role(models.TextChoices):
    SUPER_ADMIN = 'SUPER_ADMIN', 'Super Admin'
    SUPER_DISTRIBUTOR = 'SUPER_DISTRIBUTOR', 'Super Distributor / Super Seller'
    DISTRIBUTOR = 'DISTRIBUTOR', 'Distributor'
    SUB_DISTRIBUTOR = 'SUB_DISTRIBUTOR', 'Sub Distributor'
    RETAILER = 'RETAILER', 'Retailer'
    CUSTOMER = 'CUSTOMER', 'Customer'
    STAFF_OPERATIONS = 'STAFF_OPERATIONS', 'Staff - Operations'
    STAFF_COLLECTIONS = 'STAFF_COLLECTIONS', 'Staff - Collections'
    STAFF_SUPPORT = 'STAFF_SUPPORT', 'Staff - Technical Support'
    STAFF_VIEWER = 'STAFF_VIEWER', 'Staff - Auditor / Viewer'

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

    # Partner Management Fields
    approval_step = models.CharField(
        max_length=20,
        default='ACTIVE',
        choices=[
            ('PENDING', 'Pending'),
            ('ACTIVE', 'Active'),
            ('REJECTED', 'Rejected'),
            ('SUSPENDED', 'Suspended')
        ]
    )
    suspend_reason = models.TextField(null=True, blank=True)
    can_add_partners = models.BooleanField(default=True)
    can_see_devices = models.BooleanField(default=True)
    can_see_customer_phones = models.BooleanField(default=True)

    # Retailer Specific Fields
    store_phone = models.CharField(max_length=20, null=True, blank=True)
    district = models.CharField(max_length=100, null=True, blank=True)
    allowed_devices = models.JSONField(default=list, blank=True)
    grace_period = models.PositiveIntegerField(default=3)
    max_devices = models.PositiveIntegerField(default=100)

    # Customer Specific Fields
    id_type = models.CharField(max_length=50, null=True, blank=True)
    id_number = models.CharField(max_length=50, null=True, blank=True)
    id_front_image = models.FileField(upload_to='kyc_documents/', null=True, blank=True)
    id_back_image = models.FileField(upload_to='kyc_documents/', null=True, blank=True)
    signed_emi_agreement_flag = models.BooleanField(default=False)
    draft_status = models.CharField(
        max_length=20,
        default='COMPLETED',
        choices=[('DRAFT', 'Draft'), ('COMPLETED', 'Completed')]
    )
    
    objects = UserManager()

    def get_all_subordinates(self):
        """Returns a flat list of all downstream subordinate users under this user."""
        if self.role == 'SUPER_ADMIN':
            return list(User.objects.exclude(id=self.id))
        subordinates = list(self.children.all())
        for sub in subordinates:
            subordinates.extend(sub.get_all_subordinates())
        return subordinates

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


class NotificationType(models.TextChoices):
    KEY_REQUEST = 'KEY_REQUEST', 'Key Request'
    PAYMENT_RECEIVED = 'PAYMENT_RECEIVED', 'Payment Received'
    OVERDUE_ALERT = 'OVERDUE_ALERT', 'Overdue Alert'
    DEVICE_LOCK = 'DEVICE_LOCK', 'Device Lock'
    DEVICE_UNLOCK = 'DEVICE_UNLOCK', 'Device Unlock'
    SYSTEM = 'SYSTEM', 'System Alert'


class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    notification_type = models.CharField(max_length=50, choices=NotificationType.choices, default=NotificationType.SYSTEM)
    reference_id = models.CharField(max_length=100, null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} for {self.recipient.username}"


class PlatformSettings(models.Model):
    company_name = models.CharField(max_length=255, default='Prime Locker')
    support_phone = models.CharField(max_length=50, default='+91 8000-111-222')
    support_email = models.CharField(max_length=100, default='support@primelocker.pro')
    company_address = models.TextField(default='100 MG Road, Bengaluru, Karnataka 560001')
    gstin = models.CharField(max_length=50, null=True, blank=True, default='29AAAAA0000A1Z5')
    default_grace_period_days = models.PositiveIntegerField(default=3)
    auto_lock_enabled = models.BooleanField(default=True)
    late_fee_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    # Third-party integrations
    razorpay_key_id = models.CharField(max_length=100, null=True, blank=True)
    razorpay_key_secret = models.CharField(max_length=100, null=True, blank=True)
    whatsapp_provider = models.CharField(max_length=50, default='SIMULATION')
    whatsapp_api_key = models.CharField(max_length=255, null=True, blank=True)
    whatsapp_sender_id = models.CharField(max_length=50, null=True, blank=True)
    
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"{self.company_name} Settings"


class WhatsAppMessageLog(models.Model):
    recipient_phone = models.CharField(max_length=20)
    recipient_name = models.CharField(max_length=150, null=True, blank=True)
    template_name = models.CharField(max_length=100)
    parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, default='SENT') # 'SENT', 'FAILED', 'SIMULATED'
    provider = models.CharField(max_length=50, default='SIMULATION')
    response_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.template_name} to {self.recipient_phone} ({self.status})"

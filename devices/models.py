from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import random
import string

User = get_user_model()


class DeviceCategory(models.TextChoices):
    MOBILE = 'MOBILE', 'Mobile Phone'
    TV = 'TV', 'Smart TV'


class OSType(models.TextChoices):
    ANDROID = 'ANDROID', 'Android'
    IOS = 'IOS', 'iOS (iPhone)'
    SMART_TV = 'SMART_TV', 'Smart TV OS'


class DeviceScannerState(models.TextChoices):
    NEW = 'NEW', 'New Device (Purchased Item)'
    RUNNING = 'RUNNING', 'Running Device (Customer Collateral)'


class DeviceLockStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Active (Unlocked)'
    LOCKED = 'LOCKED', 'Locked'
    UNENROLLED = 'UNENROLLED', 'Unenrolled / Released'


class LockProvider(models.TextChoices):
    ROCKETPAY = 'ROCKETPAY', 'RocketPay DLC'
    OWN_AGENT = 'OWN_AGENT', 'Prime Locker Agent'
    APPLE_MDM = 'APPLE_MDM', 'Apple MDM'
    NONE = 'NONE', 'None / Uncontrolled'


class LoanProvider(models.TextChoices):
    BAJAJ_FINANCE = 'BAJAJ_FINANCE', 'Bajaj Finance'
    TVS_CREDIT = 'TVS_CREDIT', 'TVS Credit'
    HDB_FINANCIAL = 'HDB_FINANCIAL', 'HDB Financial Services'
    IDFC_FIRST_BANK = 'IDFC_FIRST_BANK', 'IDFC FIRST Bank'
    HOME_CREDIT = 'HOME_CREDIT', 'Home Credit'
    DMI_FINANCE = 'DMI_FINANCE', 'DMI Finance'
    POONAWALLA_FINCORP = 'POONAWALLA_FINCORP', 'Poonawalla Fincorp'
    HDFC_BANK = 'HDFC_BANK', 'HDFC Bank'
    ICICI_BANK = 'ICICI_BANK', 'ICICI Bank'
    TATA_CAPITAL = 'TATA_CAPITAL', 'Tata Capital'
    AXIS_BANK = 'AXIS_BANK', 'Axis Bank'
    KOTAK_MAHINDRA_BANK = 'KOTAK_MAHINDRA_BANK', 'Kotak Mahindra Bank'
    SBI = 'SBI', 'SBI (State Bank of India)'
    CANARA_BANK = 'CANARA_BANK', 'Canara Bank'
    FEDERAL_BANK = 'FEDERAL_BANK', 'Federal Bank'
    INDUSIND_BANK = 'INDUSIND_BANK', 'IndusInd Bank'
    RETAILER_FINANCE = 'RETAILER_FINANCE', 'Retailer Finance'
    OTHER_BANK_NBFC = 'OTHER_BANK_NBFC', 'Other Bank / NBFC'
    NO_LOAN_CASH = 'NO_LOAN_CASH', 'No Loan / Cash Purchase'


class CommandType(models.TextChoices):
    LOCK = 'LOCK', 'Lock Device'
    UNLOCK = 'UNLOCK', 'Unlock Device'
    REBOOT = 'REBOOT', 'Remote Reboot'
    SYNC_LOCATION = 'SYNC_LOCATION', 'Request Live GPS Location'
    UPDATE_POLICY = 'UPDATE_POLICY', 'Update MDM Policies'
    SET_WALLPAPER = 'SET_WALLPAPER', 'Change Lock Wallpaper'
    PUSH_MESSAGE = 'PUSH_MESSAGE', 'Push Screen Reminder'
    WIPE = 'WIPE', 'Factory Wipe / Reset'


class CommandStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    EXECUTED = 'EXECUTED', 'Executed'
    FAILED = 'FAILED', 'Failed'


class AppType(models.TextChoices):
    ANDROID_LOCKER = 'ANDROID_LOCKER', 'Android Locker APK'
    TV_LOCKER = 'TV_LOCKER', 'Smart TV Locker APK'
    RETAILER_APP = 'RETAILER_APP', 'Retailer Management App'
    IOS_AGENT = 'IOS_AGENT', 'iOS / Apple DEP Profile'


def generate_offline_code():
    """Generates a secure 8-digit random pin for offline unlock"""
    return ''.join(random.choices(string.digits, k=8))


class Device(models.Model):
    """
    Core Device Registry supporting Mobiles (Android, iPhone) and Smart TVs.
    Includes MDM Policy Controls, Live Location, and Online Status tracking.
    """
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registered_devices')
    retailer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='retailer_devices')
    
    device_category = models.CharField(max_length=20, choices=DeviceCategory.choices, default=DeviceCategory.MOBILE)
    os_type = models.CharField(max_length=20, choices=OSType.choices, default=OSType.ANDROID)
    device_state = models.CharField(
        max_length=20, 
        choices=DeviceScannerState.choices, 
        default=DeviceScannerState.NEW,
        help_text="NEW = Just purchased, RUNNING = Existing phone held as collateral"
    )
    
    # Hardware IDs
    imei = models.CharField(max_length=50, unique=True, db_index=True)
    secondary_imei = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    brand = models.CharField(max_length=100, blank=True, null=True)
    model = models.CharField(max_length=100, blank=True, null=True)
    device_name = models.CharField(max_length=150, blank=True, null=True)
    
    # Financial & Activation Links
    activation_key = models.ForeignKey(
        'keys.DeviceKey', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='enrolled_device'
    )
    emi_contract = models.ForeignKey(
        'emi.CustomerEMIContract', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='enrolled_devices'
    )
    
    # Loan / Financier Details (Screen: Customer Device Details)
    loan_provider = models.CharField(
        max_length=50, 
        choices=LoanProvider.choices, 
        default=LoanProvider.NO_LOAN_CASH,
        help_text="Financier or Bank providing the loan"
    )
    loan_provider_other = models.CharField(max_length=150, blank=True, null=True, help_text="Custom name if Other Bank/NBFC")
    loan_account_number = models.CharField(max_length=100, blank=True, null=True, help_text="Loan Application / Account Number")
    
    # Status & Connectivity
    status = models.CharField(max_length=20, choices=DeviceLockStatus.choices, default=DeviceLockStatus.ACTIVE)
    last_online_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.CharField(max_length=45, blank=True, null=True)
    app_version = models.CharField(max_length=50, blank=True, null=True)
    battery_level = models.IntegerField(null=True, blank=True)
    
    # MDM Remote Policy Controls (Android & iPhone Control Suite)
    block_factory_reset = models.BooleanField(default=True, help_text="Blocks unauthorized factory reset")
    frp_protection_enabled = models.BooleanField(default=True, help_text="Factory Reset Protection lock")
    camera_disabled = models.BooleanField(default=False, help_text="Remotely disable device camera")
    social_media_disabled = models.BooleanField(default=False, help_text="Master toggle for social media apps")
    whatsapp_disabled = models.BooleanField(default=False)
    instagram_disabled = models.BooleanField(default=False)
    facebook_disabled = models.BooleanField(default=False)
    
    # Wallpaper & Screen Reminders
    wallpaper_locked = models.BooleanField(default=False)
    custom_wallpaper = models.FileField(upload_to='device_wallpapers/', null=True, blank=True)
    custom_wallpaper_url = models.URLField(blank=True, null=True)
    lock_message = models.TextField(
        default="This device is locked due to overdue payment. Please contact your retailer to restore access."
    )
    
    # Emergency Offline Unlock Code
    offline_unlock_code = models.CharField(max_length=16, default=generate_offline_code)
    offline_code_expires_at = models.DateTimeField(null=True, blank=True)
    offline_code_expiry_type = models.CharField(max_length=30, blank=True, null=True)

    # Lock Provider & Third-Party Integration (Section B)
    lock_provider = models.CharField(
        max_length=30, 
        choices=LockProvider.choices, 
        default=LockProvider.OWN_AGENT,
        help_text="Underlying lock engine (RocketPay OEM DLC, Prime Agent, Apple MDM)"
    )
    provider_device_id = models.CharField(
        max_length=150, 
        blank=True, 
        null=True, 
        help_text="Device ID assigned by the lock provider (e.g. RocketPay)"
    )
    provider_status = models.CharField(
        max_length=50, 
        blank=True, 
        null=True, 
        help_text="Status reported by provider (e.g. ENROLLED, LOCKED, UNLOCKED)"
    )

    # Device Agent Commands & Control Extensions (Section C: M10)
    installed_apps = models.JSONField(default=list, blank=True)
    locked_packages = models.JSONField(default=list, blank=True)
    streaming_blocked = models.BooleanField(default=False)
    
    # Live GPS Location Tracking
    current_latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    current_longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    current_address = models.TextField(blank=True, null=True)
    last_location_at = models.DateTimeField(null=True, blank=True)
    
    # Synced Owner Contact Details
    synced_contacts = models.JSONField(default=list, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.device_name or self.model or self.brand or 'Device'} ({self.imei}) - {self.status}"

    @property
    def is_online(self):
        """Device is considered online if it pinged the backend within the last 15 minutes"""
        if not self.last_online_at:
            return False
        return (timezone.now() - self.last_online_at) <= timedelta(minutes=15)


class DeviceCommand(models.Model):
    """
    Queue of remote commands sent to devices (Lock, Unlock, Reboot, Policy change, etc.)
    """
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='commands')
    command_type = models.CharField(max_length=30, choices=CommandType.choices)
    parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=CommandStatus.choices, default=CommandStatus.PENDING)
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_commands')
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Command {self.command_type} on {self.device.imei} - {self.status}"


class DeviceLocationLog(models.Model):
    """
    Historical log of device GPS coordinates for live location tracking.
    """
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='location_history')
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)
    accuracy = models.FloatField(null=True, blank=True)
    address = models.TextField(blank=True, null=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-recorded_at']

    def __str__(self):
        return f"Location for {self.device.imei} at {self.recorded_at}"


class AppVersion(models.Model):
    """
    Module 2: APK and App Versions Management.
    Enables Super Admin to upload, release, and distribute Android Locker, TV Locker, and Retailer apps.
    """
    app_type = models.CharField(max_length=30, choices=AppType.choices, default=AppType.ANDROID_LOCKER)
    version_name = models.CharField(max_length=50, help_text="e.g. 1.2.0")
    version_code = models.PositiveIntegerField(help_text="e.g. 120")
    apk_file = models.FileField(upload_to='apks/', null=True, blank=True)
    external_download_url = models.URLField(blank=True, null=True, help_text="Alternative CDN/Cloud direct URL")
    release_notes = models.TextField(blank=True, null=True)
    is_mandatory = models.BooleanField(default=False, help_text="Force app update before continuing")
    is_active = models.BooleanField(default=True, help_text="Latest active release for this app type")
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_apks')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-version_code', '-created_at']

    def __str__(self):
        return f"{self.get_app_type_display()} v{self.version_name} ({self.version_code}) - {'Active' if self.is_active else 'Inactive'}"


class LockMessageTemplate(models.Model):
    """
    Screen A-40 Lock Message Templates.
    Pre-configured messages displayed on locked phone/TV screens.
    """
    title = models.CharField(max_length=150)
    text = models.TextField()
    is_default = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='lock_templates')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f"{self.title}{' (Default)' if self.is_default else ''}"

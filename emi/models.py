from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import calendar

User = get_user_model()

class DeviceType(models.TextChoices):
    MOBILE = 'MOBILE', 'Mobile'
    TV = 'TV', 'Smart TV'
    ALL = 'ALL', 'All Devices'

class DeviceState(models.TextChoices):
    NEW = 'NEW', 'New Device'
    RUNNING = 'RUNNING', 'Running Device (Collateral)'

class EMIContractStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    ACTIVE = 'ACTIVE', 'Active'
    LOCKED = 'LOCKED', 'Locked (Overdue)'
    COMPLETED = 'COMPLETED', 'Completed (Fully Paid)'
    DEFAULTED = 'DEFAULTED', 'Defaulted'

class InstallmentStatus(models.TextChoices):
    UPCOMING = 'UPCOMING', 'Upcoming'
    DUE = 'DUE', 'Due'
    PAID = 'PAID', 'Paid'
    OVERDUE = 'OVERDUE', 'Overdue'

class PaymentMode(models.TextChoices):
    CASH = 'CASH', 'Cash'
    UPI = 'UPI', 'UPI'
    CARD = 'CARD', 'Debit/Credit Card'
    ONLINE_GATEWAY = 'ONLINE_GATEWAY', 'Online Gateway'


class EMIPlanTemplate(models.Model):
    """
    Master EMI Plans configured by Super-Admin .
    Retailers choose from these templates when setting up an EMI.
    """
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True, null=True)
    device_type = models.CharField(max_length=20, choices=DeviceType.choices, default=DeviceType.ALL)
    tenure_months = models.PositiveIntegerField(help_text="Tenure in months, e.g. 6, 9, 12, 18")
    
    # Financial Terms
    min_down_payment_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.00,
        help_text="Minimum down payment percentage (e.g. 10.00 for 10%)"
    )
    processing_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    interest_rate_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.00,
        help_text="Markup / Interest percentage over tenure (e.g. 0.00 for 0% EMI)"
    )
    
    # Lock & Grace Rules
    grace_period_days = models.PositiveIntegerField(
        default=3,
        help_text="Days after due date before device auto-locks"
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_emi_templates')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.tenure_months}M) - {'Active' if self.is_active else 'Inactive'}"


class CustomerEMIContract(models.Model):
    """
    Represents an active EMI financing agreement between Customer and Retailer .
    """
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='emi_contracts')
    retailer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='retailer_emi_contracts')
    plan_template = models.ForeignKey(EMIPlanTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name='contracts')
    
    # Device details (purchased product or default device)
    device_imei = models.CharField(max_length=50, db_index=True)
    device_name = models.CharField(max_length=150, blank=True, null=True)
    device_type = models.CharField(max_length=20, choices=DeviceType.choices, default=DeviceType.MOBILE)
    device_state = models.CharField(max_length=20, choices=DeviceState.choices, default=DeviceState.NEW)
    device_brand = models.CharField(max_length=100, blank=True, null=True)
    device_model = models.CharField(max_length=100, blank=True, null=True)
    
    # TV / Appliance Collateral: Lock device (FK to phone that gets locked)
    lock_device = models.ForeignKey(
        'devices.Device',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='secured_contracts',
        help_text="Customer phone that gets locked if this EMI defaults (can secure TV/Appliance EMIs)"
    )

    # Financed Product Details (stored separately from the collateral lock device)
    product_type = models.CharField(
        max_length=50, 
        default='PHONE', 
        choices=[
            ('PHONE', 'Smartphone'),
            ('TV', 'Television'),
            ('APPLIANCE', 'Home Appliance'),
            ('OTHER', 'Other Product')
        ],
        help_text="Type of financed item"
    )
    product_brand = models.CharField(max_length=100, blank=True, null=True)
    product_model = models.CharField(max_length=100, blank=True, null=True)
    product_serial_number = models.CharField(max_length=100, blank=True, null=True)
    product_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    
    # Loan Financier
    loan_provider = models.CharField(max_length=50, default='NO_LOAN_CASH')
    loan_provider_other = models.CharField(max_length=150, blank=True, null=True)
    loan_account_number = models.CharField(max_length=100, blank=True, null=True)
    
    # Financial breakdown
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Total device retail price")
    down_payment = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    processing_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    financed_amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="(Total - Down Payment + Fee + Interest)")
    tenure_months = models.PositiveIntegerField(default=12)
    monthly_emi_amount = models.DecimalField(max_digits=12, decimal_places=2)
    first_emi_date = models.DateField()
    
    # Automation & Lock rules
    auto_lock_after_grace = models.BooleanField(default=True)
    grace_period_days = models.PositiveIntegerField(default=3)
    whatsapp_reminders = models.BooleanField(default=True)
    
    status = models.CharField(max_length=20, choices=EMIContractStatus.choices, default=EMIContractStatus.ACTIVE)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"EMI #{self.id} - {self.customer.username} ({self.device_imei}) - {self.status}"

    @property
    def total_paid(self):
        return self.installments.filter(status=InstallmentStatus.PAID).aggregate(
            total=models.Sum('paid_amount')
        )['total'] or 0.00

    @property
    def total_remaining(self):
        return float(self.financed_amount) - float(self.total_paid)

    @property
    def paid_installments_count(self):
        return self.installments.filter(status=InstallmentStatus.PAID).count()

    @property
    def remaining_installments_count(self):
        return self.installments.exclude(status=InstallmentStatus.PAID).count()

    @property
    def target_lock_device(self):
        """Returns the device to lock/unlock for this contract (collateral phone or purchased device)."""
        if self.lock_device:
            return self.lock_device
        from devices.models import Device
        return Device.objects.filter(imei=self.device_imei).first()


def add_months(sourcedate, months):
    """Utility to add months while preserving day-of-month (e.g. 5th of each month)"""
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    day = min(sourcedate.day, calendar.monthrange(year, month)[1])
    return sourcedate.replace(year=year, month=month, day=day)


class EMIInstallment(models.Model):
    """
    Individual monthly installment row for the schedule.
    """
    contract = models.ForeignKey(CustomerEMIContract, on_delete=models.CASCADE, related_name='installments')
    installment_number = models.PositiveIntegerField()
    due_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, choices=InstallmentStatus.choices, default=InstallmentStatus.UPCOMING)
    
    # Collection tracking
    collected_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='collected_installments')
    payment_mode = models.CharField(max_length=30, choices=PaymentMode.choices, null=True, blank=True)
    collection_date = models.DateTimeField(null=True, blank=True)
    transaction_ref = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['contract', 'installment_number']
        unique_together = ('contract', 'installment_number')

    def __str__(self):
        return f"Installment #{self.installment_number} for Contract #{self.contract_id} - Due: {self.due_date} ({self.status})"

    @property
    def days_overdue(self):
        if self.status == InstallmentStatus.PAID:
            return 0
        today = timezone.localdate()
        if today > self.due_date:
            return (today - self.due_date).days
        return 0

    @property
    def late_fee(self):
        """Late fee calculated dynamically based on PlatformSettings.late_fee_per_day and grace period."""
        from decimal import Decimal
        if self.status == InstallmentStatus.PAID:
            return Decimal('0.00')
        from accounts.models import PlatformSettings
        settings = PlatformSettings.get_settings()
        rate = settings.late_fee_per_day or Decimal('0.00')
        if rate <= Decimal('0.00'):
            return Decimal('0.00')
        grace_days = self.contract.plan_template.grace_period_days if self.contract.plan_template else settings.default_grace_period_days
        chargeable_days = max(0, self.days_overdue - grace_days)
        return (Decimal(str(chargeable_days)) * rate).quantize(Decimal('0.01'))

    @property
    def total_due(self):
        from decimal import Decimal
        return (Decimal(str(self.amount)) + self.late_fee).quantize(Decimal('0.01'))

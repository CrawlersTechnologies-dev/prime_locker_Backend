from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()


class TargetRole(models.TextChoices):
    ALL = 'ALL', 'All Users'
    SUPER_DISTRIBUTOR = 'SUPER_DISTRIBUTOR', 'Super Distributors'
    DISTRIBUTOR = 'DISTRIBUTOR', 'Distributors'
    SUB_DISTRIBUTOR = 'SUB_DISTRIBUTOR', 'Sub-Distributors'
    RETAILER = 'RETAILER', 'Retailers'
    CUSTOMER = 'CUSTOMER', 'Customers'


class OfferType(models.TextChoices):
    KEY_BONUS = 'KEY_BONUS', 'Key Bonus / Extra Keys'
    DISCOUNT = 'DISCOUNT', 'Discount / Fee Waiver'
    FESTIVE_SCHEME = 'FESTIVE_SCHEME', 'Festive / Seasonal Scheme'
    CASHBACK = 'CASHBACK', 'Cashback / Commission Bonus'
    GENERAL = 'GENERAL', 'General Announcement / Promotion'


class Offer(models.Model):
    """
    Offers & Promotions created by Super-Admin and targeted to specific roles or everyone.
    """
    title = models.CharField(max_length=200)
    description = models.TextField()
    banner_image = models.FileField(upload_to='offer_banners/', null=True, blank=True)
    
    target_role = models.CharField(
        max_length=30,
        choices=TargetRole.choices,
        default=TargetRole.ALL,
        help_text="Select specific role or 'ALL' for everyone"
    )
    offer_type = models.CharField(
        max_length=30,
        choices=OfferType.choices,
        default=OfferType.GENERAL
    )
    
    # Optional values depending on offer type
    promo_code = models.CharField(max_length=50, blank=True, null=True, help_text="e.g. DIWALI500")
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    bonus_keys = models.PositiveIntegerField(null=True, blank=True, help_text="e.g. 10 free keys")
    min_key_purchase = models.PositiveIntegerField(null=True, blank=True, help_text="e.g. minimum 100 keys to get bonus")
    
    # Validity period
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(null=True, blank=True, help_text="Leave blank for ongoing offer")
    
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_offers')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} (Target: {self.get_target_role_display()}) - {'Active' if self.is_active else 'Inactive'}"

    @property
    def is_currently_valid(self):
        if not self.is_active:
            return False
        now = timezone.now()
        if now < self.valid_from:
            return False
        if self.valid_to and now > self.valid_to:
            return False
        return True

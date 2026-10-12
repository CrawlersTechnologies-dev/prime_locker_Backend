from rest_framework import serializers
from .models import Offer

class OfferSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField(read_only=True)
    is_currently_valid = serializers.ReadOnlyField()

    class Meta:
        model = Offer
        fields = [
            'id',
            'title',
            'description',
            'banner_image',
            'target_role',
            'offer_type',
            'promo_code',
            'discount_percentage',
            'discount_amount',
            'bonus_keys',
            'min_key_purchase',
            'valid_from',
            'valid_to',
            'is_active',
            'is_currently_valid',
            'created_by',
            'created_by_name',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            name = f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
            return name or obj.created_by.username
        return None

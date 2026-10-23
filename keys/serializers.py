from rest_framework import serializers
from .models import DeviceKey, KeyTransaction, KeyStatus, KeyRequest

class DeviceKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceKey
        fields = '__all__'

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        # Inject current owner details
        if instance.current_owner:
            owner = instance.current_owner
            owner_name = f"{owner.first_name} {owner.last_name}".strip()
            if not owner_name:
                owner_name = owner.username
            
            representation['current_owner'] = {
                'id': owner.id,
                'name': owner_name,
                'role': owner.role,
                'business_name': owner.business_name
            }
            
        # Inject activated customer details
        if instance.activated_for_customer:
            customer = instance.activated_for_customer
            customer_name = f"{customer.first_name} {customer.last_name}".strip()
            if not customer_name:
                customer_name = customer.username
                
            representation['activated_for_customer'] = {
                'id': customer.id,
                'name': customer_name,
                'mobile_number': customer.mobile_number
            }
            
        return representation

class KeyTransactionSerializer(serializers.ModelSerializer):
    key_string = serializers.CharField(source='key.key_string', read_only=True)
    device_type = serializers.CharField(source='key.device_type', read_only=True)
    from_user_details = serializers.SerializerMethodField(read_only=True)
    to_user_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = KeyTransaction
        fields = [
            'id',
            'key',
            'key_string',
            'device_type',
            'action',
            'notes',
            'from_user',
            'from_user_details',
            'to_user',
            'to_user_details',
            'timestamp'
        ]

    def get_from_user_details(self, obj):
        if obj.from_user:
            u = obj.from_user
            name = f"{u.first_name} {u.last_name}".strip() or u.username
            return {"id": u.id, "name": name, "role": u.role, "business_name": u.business_name}
        return None

    def get_to_user_details(self, obj):
        if obj.to_user:
            u = obj.to_user
            name = f"{u.first_name} {u.last_name}".strip() or u.username
            return {"id": u.id, "name": name, "role": u.role, "business_name": u.business_name}
        return None

class GenerateKeysSerializer(serializers.Serializer):
    prefix = serializers.CharField(max_length=10, required=True)
    device_type = serializers.CharField(max_length=50, required=True)
    quantity = serializers.IntegerField(min_value=1, max_value=10000, required=True)
    reference_note = serializers.CharField(required=False, allow_blank=True, default='')

class TransferKeysSerializer(serializers.Serializer):
    target_role = serializers.CharField(max_length=50, required=False, allow_blank=True)
    target_user_id = serializers.IntegerField(required=True)
    device_type = serializers.CharField(max_length=50, required=True)
    quantity = serializers.IntegerField(min_value=1, max_value=10000, required=True)
    reference_note = serializers.CharField(required=False, allow_blank=True, default='')

class ActivateKeySerializer(serializers.Serializer):
    key_string = serializers.CharField(required=True)
    device_id = serializers.CharField(max_length=100, required=True)
    customer_id = serializers.IntegerField(required=True)

class KeyRequestSerializer(serializers.ModelSerializer):
    requester_details = serializers.SerializerMethodField(read_only=True)
    platform_display = serializers.SerializerMethodField(read_only=True)
    requester_key_balance = serializers.SerializerMethodField(read_only=True)
    requester_keys_used_this_month = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = KeyRequest
        fields = '__all__'
        read_only_fields = ['requester', 'parent', 'status']

    def get_requester_details(self, obj):
        return {
            "id": obj.requester.id,
            "name": f"{obj.requester.first_name} {obj.requester.last_name}".strip() or obj.requester.username,
            "role": obj.requester.role,
            "business_name": obj.requester.business_name
        }

    def get_platform_display(self, obj):
        target = (obj.os_type or obj.device_type or '').upper()
        if target in ['IOS', 'IPHONE', 'MOBILE_IOS']:
            return "Apple iOS (iPhone)"
        elif target in ['ANDROID', 'MOBILE_ANDROID']:
            return "Android Smartphone"
        elif target in ['TV', 'SMART_TV']:
            return "Smart TV"
        elif target == 'MOBILE':
            return "Mobile Phone (Universal)"
        return target

    def get_requester_key_balance(self, obj):
        from keys.models import DeviceKey, KeyStatus
        return DeviceKey.objects.filter(current_owner=obj.requester, status=KeyStatus.AVAILABLE).count()

    def get_requester_keys_used_this_month(self, obj):
        from keys.models import KeyTransaction, KeyAction
        from django.utils import timezone
        now = timezone.now()
        return KeyTransaction.objects.filter(
            from_user=obj.requester,
            action=KeyAction.ACTIVATED,
            timestamp__year=now.year,
            timestamp__month=now.month
        ).count()




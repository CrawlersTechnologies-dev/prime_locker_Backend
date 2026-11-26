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
    batch_id = serializers.UUIDField(read_only=True)
    quantity = serializers.IntegerField(default=1, read_only=True)
    key_string = serializers.CharField(source='key.key_string', read_only=True)
    device_type = serializers.CharField(source='key.device_type', read_only=True)
    from_user_details = serializers.SerializerMethodField(read_only=True)
    to_user_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = KeyTransaction
        fields = [
            'id',
            'batch_id',
            'quantity',
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
    device_type = serializers.CharField(max_length=50, required=False, allow_blank=True)
    service = serializers.CharField(max_length=50, required=False, allow_blank=True)
    service_code = serializers.CharField(max_length=50, required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1, max_value=10000, required=True)
    reference_note = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        dev = attrs.get('device_type') or attrs.get('service') or attrs.get('service_code')
        if not dev:
            raise serializers.ValidationError({"device_type": "Please specify device_type or service to transfer."})
        from keys.services import resolve_service_code
        svc_code = resolve_service_code(service=dev)
        attrs['service_code'] = svc_code
        attrs['device_type'] = dev.upper()
        return attrs

class ActivateKeySerializer(serializers.Serializer):
    key_string = serializers.CharField(required=True)
    device_id = serializers.CharField(max_length=100, required=True)
    customer_id = serializers.IntegerField(required=True)

class KeyRequestSerializer(serializers.ModelSerializer):
    device_type = serializers.CharField(max_length=50, required=False, allow_blank=True)
    service = serializers.CharField(write_only=True, required=False, allow_blank=True)
    service_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    requester_details = serializers.SerializerMethodField(read_only=True)
    parent_details = serializers.SerializerMethodField(read_only=True)
    requester_name = serializers.SerializerMethodField(read_only=True)
    requester_username = serializers.SerializerMethodField(read_only=True)
    requester_role = serializers.SerializerMethodField(read_only=True)
    requester_business_name = serializers.SerializerMethodField(read_only=True)
    requester_mobile = serializers.SerializerMethodField(read_only=True)
    parent_name = serializers.SerializerMethodField(read_only=True)
    parent_username = serializers.SerializerMethodField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    is_pending = serializers.SerializerMethodField(read_only=True)
    is_approved = serializers.SerializerMethodField(read_only=True)
    is_rejected = serializers.SerializerMethodField(read_only=True)
    is_declined = serializers.SerializerMethodField(read_only=True)
    declined = serializers.SerializerMethodField(read_only=True)
    platform_display = serializers.SerializerMethodField(read_only=True)
    requester_key_balance = serializers.SerializerMethodField(read_only=True)
    requester_keys_used_this_month = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = KeyRequest
        fields = '__all__'
        read_only_fields = ['requester', 'parent', 'status']

    def validate(self, attrs):
        from keys.services import resolve_service_code, SERVICE_META
        service_hint = attrs.pop('service', None) or attrs.pop('service_code', None)
        if not attrs.get('device_type') and not service_hint and not attrs.get('os_type'):
            raise serializers.ValidationError({"device_type": "Please specify device_type or service."})
        resolved_code = resolve_service_code(
            service=service_hint or attrs.get('device_type'),
            os_type=attrs.get('os_type'),
        )
        if not attrs.get('device_type'):
            attrs['device_type'] = SERVICE_META[resolved_code]['device_type']
        return attrs

    def get_requester_details(self, obj):
        return {
            "id": obj.requester.id,
            "name": f"{obj.requester.first_name} {obj.requester.last_name}".strip() or obj.requester.username,
            "role": obj.requester.role,
            "business_name": obj.requester.business_name,
            "mobile_number": obj.requester.mobile_number,
        }

    def get_parent_details(self, obj):
        if not obj.parent:
            return None
        return {
            "id": obj.parent.id,
            "name": f"{obj.parent.first_name} {obj.parent.last_name}".strip() or obj.parent.username,
            "role": obj.parent.role,
            "business_name": obj.parent.business_name,
            "mobile_number": obj.parent.mobile_number,
        }

    def get_requester_name(self, obj):
        return f"{obj.requester.first_name} {obj.requester.last_name}".strip() or obj.requester.username

    def get_requester_username(self, obj):
        return obj.requester.username

    def get_requester_role(self, obj):
        return obj.requester.role

    def get_requester_business_name(self, obj):
        return obj.requester.business_name or ""

    def get_requester_mobile(self, obj):
        return obj.requester.mobile_number or ""

    def get_parent_name(self, obj):
        if not obj.parent:
            return ""
        return f"{obj.parent.first_name} {obj.parent.last_name}".strip() or obj.parent.username

    def get_parent_username(self, obj):
        return obj.parent.username if obj.parent else ""

    def get_is_pending(self, obj):
        return obj.status == 'PENDING'

    def get_is_approved(self, obj):
        return obj.status == 'APPROVED'

    def get_is_rejected(self, obj):
        return obj.status == 'REJECTED'

    def get_is_declined(self, obj):
        return obj.status == 'REJECTED'

    def get_declined(self, obj):
        return obj.status == 'REJECTED'

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




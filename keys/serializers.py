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
    class Meta:
        model = KeyTransaction
        fields = '__all__'

class GenerateKeysSerializer(serializers.Serializer):
    prefix = serializers.CharField(max_length=10, required=True)
    device_type = serializers.CharField(max_length=50, required=True)
    quantity = serializers.IntegerField(min_value=1, max_value=10000, required=True)

class TransferKeysSerializer(serializers.Serializer):
    target_user_id = serializers.IntegerField(required=True)
    device_type = serializers.CharField(max_length=50, required=True)
    quantity = serializers.IntegerField(min_value=1, max_value=10000, required=True)

class ActivateKeySerializer(serializers.Serializer):
    key_string = serializers.CharField(required=True)
    device_id = serializers.CharField(max_length=100, required=True)
    customer_id = serializers.IntegerField(required=True)

class KeyRequestSerializer(serializers.ModelSerializer):
    requester_details = serializers.SerializerMethodField(read_only=True)
    
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


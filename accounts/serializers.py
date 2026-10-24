from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import models
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Add custom claims
        token['username'] = user.username
        token['role'] = user.role
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        
        # Check if the user wants to stay signed in
        keep_me_signed_in = self.initial_data.get('keep_me_signed_in', False)
        
        # Get the refresh token object
        from rest_framework_simplejwt.tokens import RefreshToken
        from datetime import timedelta
        
        refresh = RefreshToken.for_user(self.user)
        
        # Adjust custom claims on the new refresh token
        refresh['username'] = self.user.username
        refresh['role'] = self.user.role
        
        if keep_me_signed_in:
            # Extend refresh token to 30 days
            refresh.set_exp(lifetime=timedelta(days=30))
        else:
            # Default to 1 day if they don't check the box
            refresh.set_exp(lifetime=timedelta(days=1))
            
        data['refresh'] = str(refresh)
        data['access'] = str(refresh.access_token)
        
        # Add user data to the response
        data['user'] = {
            'id': self.user.id,
            'username': self.user.username,
            'email': self.user.email,
            'role': self.user.role,
        }
        return data

class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'mobile_number', 'password', 'role', 'parent', 'first_name', 'last_name', 'is_active', 'date_joined']
        read_only_fields = ['id', 'date_joined']

    def validate_email(self, value):
        if value:
            value = value.strip().lower()
            qs = User.objects.filter(models.Q(email__iexact=value) | models.Q(username__iexact=value))
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("A user with this email is already registered.")
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        
        # DRF MultiPartParser injects 'False' for missing booleans.
        # We check initial_data to see if they actually sent it.
        if 'is_active' not in self.initial_data:
            validated_data['is_active'] = True
            
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.parent:
            parent_name = f"{instance.parent.first_name} {instance.parent.last_name}".strip()
            if not parent_name:
                parent_name = instance.parent.username
            
            representation['parent'] = {
                'id': instance.parent.id,
                'name': parent_name,
                'role': instance.parent.role,
                'business_name': instance.parent.business_name
            }
        return representation

class SuperDistributorSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(write_only=True, required=True)
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    confirm_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    opening_keys = serializers.IntegerField(write_only=True, required=False, default=0, min_value=0)
    
    # We don't require 'username' in the request, we will generate it from email
    username = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'mobile_number', 'full_name', 'business_name',
            'pan_number', 'gst_number', 'state', 'city', 'pincode', 'address',
            'kyc_document', 'remarks', 'can_reclaim_keys', 'password', 'confirm_password',
            'approval_step', 'suspend_reason', 'can_add_partners', 'can_see_devices', 'can_see_customer_phones',
            'opening_keys',
            'role', 'parent', 'is_active', 'date_joined'
        ]
        read_only_fields = ['id', 'date_joined', 'role', 'parent']
        
        extra_kwargs = {
            'email': {'required': True},
            'mobile_number': {'required': True},
            'business_name': {'required': True},
            'state': {'required': True},
            'city': {'required': True},
            'pincode': {'required': True},
            'address': {'required': True},
        }

    def validate(self, data):
        if data.get('password') != data.get('confirm_password'):
            raise serializers.ValidationError({"password": "Passwords must match."})
        return data

    def validate_email(self, value):
        if value:
            value = value.strip().lower()
            qs = User.objects.filter(models.Q(email__iexact=value) | models.Q(username__iexact=value))
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("A user with this email is already registered.")
        return value

    def create(self, validated_data):
        validated_data.pop('confirm_password')
        password = validated_data.pop('password')
        full_name = validated_data.pop('full_name')
        opening_keys = validated_data.pop('opening_keys', 0)
        
        # Parse full name
        name_parts = full_name.split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''
        
        # Auto-set username to email since UI has no username field
        validated_data['username'] = validated_data.get('email')
        
        # DRF MultiPartParser injects 'False' for missing booleans.
        # We check initial_data to see if they actually sent it.
        if 'is_active' not in self.initial_data:
            validated_data['is_active'] = True

        user = User(first_name=first_name, last_name=last_name, **validated_data)
        user.set_password(password)
        user.save()

        # Handle opening keys if requested
        if opening_keys and opening_keys > 0:
            request = self.context.get('request')
            if request and request.user and request.user.is_authenticated:
                creator = request.user
                from keys.models import DeviceKey, KeyStatus, KeyTransaction, KeyAction
                from keys.views import generate_random_string

                creator_keys = list(DeviceKey.objects.filter(current_owner=creator, status=KeyStatus.AVAILABLE)[:opening_keys])
                if len(creator_keys) < opening_keys and creator.role == 'SUPER_ADMIN':
                    needed = opening_keys - len(creator_keys)
                    new_keys = [
                        DeviceKey(
                            key_string=f"MOB-{generate_random_string(8)}",
                            device_type="MOBILE",
                            status=KeyStatus.AVAILABLE,
                            current_owner=creator
                        ) for _ in range(needed)
                    ]
                    DeviceKey.objects.bulk_create(new_keys)
                    creator_keys = list(DeviceKey.objects.filter(current_owner=creator, status=KeyStatus.AVAILABLE)[:opening_keys])

                keys_to_transfer = creator_keys[:opening_keys]
                for k in keys_to_transfer:
                    k.current_owner = user
                    k.save(update_fields=['current_owner'])
                    KeyTransaction.objects.create(
                        key=k,
                        from_user=creator,
                        to_user=user,
                        action=KeyAction.ALLOCATED,
                        notes=f"Initial opening keys allocation ({len(keys_to_transfer)} keys)"
                    )

        return user

    def update(self, instance, validated_data):
        if 'password' in validated_data:
            password = validated_data.pop('password')
            validated_data.pop('confirm_password', None)
            instance.set_password(password)
        
        # We don't want to update full_name directly since it's split
        if 'full_name' in validated_data:
            full_name = validated_data.pop('full_name')
            name_parts = full_name.split(' ', 1)
            instance.first_name = name_parts[0]
            instance.last_name = name_parts[1] if len(name_parts) > 1 else ''
            
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        # Name fields
        representation['first_name'] = instance.first_name or ''
        representation['last_name'] = instance.last_name or ''
        full_name = f"{instance.first_name} {instance.last_name}".strip()
        representation['name'] = full_name or instance.business_name or instance.username

        if instance.parent:
            parent_name = f"{instance.parent.first_name} {instance.parent.last_name}".strip()
            if not parent_name:
                parent_name = instance.parent.username
            
            representation['parent'] = {
                'id': instance.parent.id,
                'name': parent_name,
                'role': instance.parent.role,
                'business_name': instance.parent.business_name
            }

        # Available keys per device type & total
        from keys.models import DeviceKey, KeyStatus, KeyTransaction, KeyAction
        from django.db.models import Count

        owned_keys = DeviceKey.objects.filter(current_owner=instance, status=KeyStatus.AVAILABLE)
        type_counts = dict(owned_keys.values('device_type').annotate(c=Count('id')).values_list('device_type', 'c'))
        total_available = sum(type_counts.values())
        type_counts['total'] = total_available
        representation['available_keys'] = type_counts

        # Key transaction totals
        keys_rec = KeyTransaction.objects.filter(to_user=instance).exclude(from_user=instance).count()
        keys_sent = KeyTransaction.objects.filter(from_user=instance, action=KeyAction.ALLOCATED).count()
        keys_used = DeviceKey.objects.filter(current_owner=instance, status=KeyStatus.ACTIVATED).count() + KeyTransaction.objects.filter(from_user=instance, action=KeyAction.ACTIVATED).count()
        representation['keys_received'] = keys_rec
        representation['keys_sent'] = keys_sent
        representation['keys_used'] = keys_used

        # Counts of downstream children
        downstream = instance.get_all_subordinates()
        representation['counts_of_children'] = {
            'distributors': sum(1 for u in downstream if u.role == 'DISTRIBUTOR'),
            'sub_distributors': sum(1 for u in downstream if u.role == 'SUB_DISTRIBUTOR'),
            'retailers': sum(1 for u in downstream if u.role == 'RETAILER'),
            'customers': sum(1 for u in downstream if u.role == 'CUSTOMER'),
            'total': len(downstream)
        }

        return representation

class DistributorSerializer(SuperDistributorSerializer):
    """Inherits identical fields from SuperDistributorSerializer"""
    pass

class SubDistributorSerializer(SuperDistributorSerializer):
    """Inherits identical fields from SuperDistributorSerializer"""
    pass

class RetailerSerializer(SuperDistributorSerializer):
    """Retailer uses similar fields plus store/device configurations"""
    class Meta(SuperDistributorSerializer.Meta):
        fields = SuperDistributorSerializer.Meta.fields + [
            'store_phone', 'district', 'allowed_devices', 'grace_period', 'max_devices'
        ]

class CustomerSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(write_only=True, required=False)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    name = serializers.SerializerMethodField(read_only=True)
    masked_id_number = serializers.SerializerMethodField(read_only=True)
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    device_summary = serializers.SerializerMethodField(read_only=True)
    contract_summary = serializers.SerializerMethodField(read_only=True)
    emi_status = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'first_name', 'last_name', 'name', 'full_name', 'email', 'mobile_number', 'alternate_mobile_number',
            'address', 'city', 'state', 'pincode', 
            'customer_photo', 'customer_signature',
            'id_type', 'id_number', 'masked_id_number', 'id_front_image', 'id_back_image',
            'signed_emi_agreement_flag', 'draft_status',
            'password', 'parent', 'role', 'is_active', 'date_joined',
            'device_summary', 'contract_summary', 'emi_status'
        ]
        read_only_fields = [
            'id', 'date_joined', 'role', 'parent', 'is_active', 'masked_id_number',
            'device_summary', 'contract_summary', 'emi_status'
        ]

    def get_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username

    def get_masked_id_number(self, obj):
        if obj.id_number:
            raw = str(obj.id_number).strip()
            if len(raw) > 4:
                return ('X' * (len(raw) - 4)) + raw[-4:]
            return raw
        return None

    def get_device_summary(self, obj):
        device = obj.registered_devices.first()
        if not device:
            return None
        return {
            "id": device.id,
            "imei": device.imei,
            "brand": device.brand,
            "model": device.model,
            "device_name": device.device_name,
            "device_category": device.device_category,
            "status": device.status,
            "is_online": device.is_online,
            "lock_provider": device.lock_provider,
            "provider_device_id": device.provider_device_id,
            "provider_status": device.provider_status
        }

    def get_contract_summary(self, obj):
        contract = getattr(obj, 'emi_contracts', None) and obj.emi_contracts.first()
        if not contract:
            return None
        from emi.models import InstallmentStatus
        overdue_count = contract.installments.filter(status=InstallmentStatus.OVERDUE).count()
        next_inst = contract.installments.filter(
            status__in=[InstallmentStatus.UPCOMING, InstallmentStatus.DUE]
        ).order_by('due_date').first()
        return {
            "id": contract.id,
            "status": contract.status,
            "total_amount": str(contract.total_amount),
            "paid_amount": str(contract.total_paid),
            "remaining_amount": str(contract.total_remaining),
            "overdue_count": overdue_count,
            "next_due_date": next_inst.due_date if next_inst else None
        }

    def get_emi_status(self, obj):
        contract = getattr(obj, 'emi_contracts', None) and obj.emi_contracts.first()
        if not contract:
            return 'no_emi'
        if contract.status == 'COMPLETED':
            return 'completed'
        from emi.models import InstallmentStatus
        has_overdue = contract.installments.filter(status=InstallmentStatus.OVERDUE).exists()
        if has_overdue or contract.status == 'LOCKED':
            return 'overdue'
        return 'on_emi'

    def validate_email(self, value):
        if value:
            value = value.strip().lower()
            qs = User.objects.filter(models.Q(email__iexact=value) | models.Q(username__iexact=value))
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("A user with this email is already registered.")
        return value

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['first_name'] = instance.first_name or ''
        representation['last_name'] = instance.last_name or ''
        representation['name'] = f"{instance.first_name} {instance.last_name}".strip() or instance.username
        
        # Format parent to be nested
        if instance.parent:
            parent = instance.parent
            parent_name = f"{parent.first_name} {parent.last_name}".strip()
            if not parent_name:
                parent_name = parent.username
            
            representation['parent'] = {
                'id': parent.id,
                'name': parent_name,
                'role': parent.role,
                'business_name': parent.business_name
            }
            
        return representation

    def create(self, validated_data):
        password = validated_data.pop('password')
        full_name = validated_data.pop('full_name', None)
        
        if full_name:
            name_parts = full_name.split(' ', 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ''
            validated_data['first_name'] = first_name
            validated_data['last_name'] = last_name
        
        validated_data['username'] = validated_data.get('email')
        
        if 'is_active' not in self.initial_data:
            validated_data['is_active'] = True

        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

class PasswordResetRequestSerializer(serializers.Serializer):
    # This can accept email, mobile number, or username
    username = serializers.CharField(required=True)

class PasswordResetConfirmSerializer(serializers.Serializer):
    username = serializers.CharField(required=True)
    otp = serializers.CharField(required=True, max_length=6)
    new_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    confirm_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    def validate(self, data):
        if data['new_password'] != data['confirm_password']:
            raise serializers.ValidationError({"password": "Passwords must match."})
        return data

class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True)
    confirm_password = serializers.CharField(required=True, write_only=True)

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Incorrect current password.")
        return value

    def validate(self, data):
        if data['new_password'] != data['confirm_password']:
            raise serializers.ValidationError({"confirm_password": "New passwords do not match."})
        if len(data['new_password']) < 6:
            raise serializers.ValidationError({"new_password": "Password must be at least 6 characters long."})
        return data

from .models import AuditLog

class AuditLogSerializer(serializers.ModelSerializer):
    user_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'user',
            'user_details',
            'action',
            'ip_address',
            'timestamp',
            'details'
        ]

    def get_user_details(self, obj):
        if obj.user:
            u = obj.user
            name = f"{u.first_name} {u.last_name}".strip() or u.username
            return {
                'id': u.id,
                'name': name,
                'username': u.username,
                'role': u.role,
                'business_name': u.business_name
            }
        return None


from .models import Notification, PlatformSettings, WhatsAppMessageLog

class NotificationSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'recipient', 'sender', 'sender_name', 'title', 'message',
            'notification_type', 'reference_id', 'is_read', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

    def get_sender_name(self, obj):
        if obj.sender:
            return f"{obj.sender.first_name} {obj.sender.last_name}".strip() or obj.sender.username
        return "System"


class PlatformSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformSettings
        fields = '__all__'

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        request = self.context.get('request')
        is_super_admin = request and request.user and request.user.role == 'SUPER_ADMIN'
        if not is_super_admin:
            representation.pop('razorpay_key_secret', None)
            representation.pop('whatsapp_api_key', None)
        return representation


class StaffUserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(required=False)
    full_name = serializers.CharField(write_only=True, required=False)
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'full_name', 'first_name', 'last_name',
            'mobile_number', 'role', 'is_active', 'date_joined', 'password'
        ]
        read_only_fields = ['id', 'date_joined']

    def validate_role(self, value):
        valid_staff_roles = ['STAFF_OPERATIONS', 'STAFF_COLLECTIONS', 'STAFF_SUPPORT', 'STAFF_VIEWER']
        if value not in valid_staff_roles:
            raise serializers.ValidationError(f"Invalid staff role. Choices are: {', '.join(valid_staff_roles)}")
        return value

    def validate_email(self, value):
        if value:
            value = value.strip().lower()
            qs = User.objects.filter(models.Q(email__iexact=value) | models.Q(username__iexact=value))
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("A user with this email is already registered.")
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        full_name = validated_data.pop('full_name', '')
        if full_name:
            parts = full_name.split(' ', 1)
            validated_data['first_name'] = parts[0]
            validated_data['last_name'] = parts[1] if len(parts) > 1 else ''
        
        if not validated_data.get('username'):
            validated_data['username'] = validated_data.get('email')

        user = User(**validated_data)
        user.set_password(password)
        user.is_staff = True
        user.save()
        return user


class WhatsAppMessageLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppMessageLog
        fields = '__all__'


class BannerSerializer(serializers.ModelSerializer):
    """
    Serializer for Banner Management (Screen A-42).
    """
    class Meta:
        from .models import Banner
        model = Banner
        fields = [
            'id', 'title', 'image', 'link', 'target_role',
            'is_active', 'order', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


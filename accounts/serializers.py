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
        data['has_mpin'] = bool(getattr(self.user, 'has_mpin', False))

        # Add user data to the response
        data['user'] = build_auth_user_payload(self.user)
        return data


def build_auth_user_payload(user):
    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'role': user.role,
        'first_name': user.first_name or '',
        'last_name': user.last_name or '',
        'mobile_number': user.mobile_number,
        'business_name': user.business_name,
        'has_mpin': bool(getattr(user, 'has_mpin', False)),
    }


def build_login_response_data(user, keep_me_signed_in=True):
    refresh = CustomTokenObtainPairSerializer.get_token(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
        'has_mpin': bool(getattr(user, 'has_mpin', False)),
        'keep_me_signed_in': bool(keep_me_signed_in),
        'user': build_auth_user_payload(user),
    }


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'mobile_number', 'password', 'role', 'parent',
            'first_name', 'last_name', 'business_name', 'has_mpin', 'is_active', 'date_joined'
        ]
        read_only_fields = ['id', 'has_mpin', 'date_joined']

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

def build_retailer_bank_mandate_summary(retailer, include_unmasked=False):
    """Builds synced bank & UPI e-Mandate summary for Retailer Profile and Admin Controls."""
    from django.core.exceptions import ObjectDoesNotExist
    retailer_name = f"{retailer.first_name} {retailer.last_name}".strip() or retailer.business_name or retailer.username
    try:
        bank_acct = retailer.bank_account
    except ObjectDoesNotExist:
        bank_acct = None

    if not bank_acct:
        return {
            'retailer_id': retailer.id,
            'retailer_code': f'RET-{retailer.id:04d}',
            'retailer_name': retailer_name,
            'mobile_number': retailer.mobile_number,
            'has_bank_details': False,
            'account_holder_name': None,
            'bank_name': None,
            'account_number_masked': None,
            'ifsc_code': None,
            'upi_id_masked': None,
            'verification_status': 'PENDING',
            'verified_at': None,
            'verification_ref': None,
            'verification_remarks': None,
            'upi_mandate_status': 'PENDING',
            'mandate_registration_date': None,
            'mandate_reference_id': None,
            'last_mandate_transaction_status': 'NONE',
            'last_transaction_at': None,
            'mandate_id': None,
            'mandate_auth_url': None,
        }

    summary = {
        'retailer_id': retailer.id,
        'retailer_code': f'RET-{retailer.id:04d}',
        'retailer_name': retailer_name,
        'mobile_number': bank_acct.mobile_number or retailer.mobile_number,
        'has_bank_details': True,
        'account_holder_name': bank_acct.account_holder_name,
        'bank_name': bank_acct.bank_name,
        'account_number_masked': bank_acct.masked_account_number,
        'ifsc_code': bank_acct.ifsc_code,
        'upi_id_masked': bank_acct.masked_upi_id,
        'verification_status': bank_acct.verification_status,
        'verified_at': bank_acct.verified_at,
        'verification_ref': bank_acct.verification_ref,
        'verification_remarks': bank_acct.verification_remarks,
        'upi_mandate_status': bank_acct.mandate_status,
        'mandate_registration_date': bank_acct.mandate_registered_at,
        'mandate_reference_id': bank_acct.mandate_reference_id,
        'last_mandate_transaction_status': bank_acct.last_transaction_status,
        'last_transaction_at': bank_acct.last_transaction_at,
        'mandate_id': bank_acct.mandate_id,
        'mandate_auth_url': bank_acct.mandate_auth_url,
    }
    if include_unmasked:
        summary['account_number'] = bank_acct.account_number
        summary['upi_id'] = bank_acct.upi_id
    return summary


class RetailerSerializer(SuperDistributorSerializer):
    """Retailer uses similar fields plus store/device configurations and synced bank/mandate summary."""
    class Meta(SuperDistributorSerializer.Meta):
        fields = SuperDistributorSerializer.Meta.fields + [
            'store_phone', 'district', 'allowed_devices', 'grace_period', 'max_devices'
        ]

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        summary = build_retailer_bank_mandate_summary(instance, include_unmasked=False)
        rep['retailer_code'] = summary['retailer_code']
        rep['bank_verification_status'] = summary['verification_status']
        rep['upi_mandate_status'] = summary['upi_mandate_status']
        rep['mandate_reference_id'] = summary['mandate_reference_id']
        rep['mandate_registration_date'] = summary['mandate_registration_date']
        rep['last_mandate_transaction_status'] = summary['last_mandate_transaction_status']
        rep['bank_mandate_details'] = summary
        return rep

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
    status = serializers.SerializerMethodField(read_only=True)
    customer_status = serializers.SerializerMethodField(read_only=True)
    can_edit = serializers.SerializerMethodField(read_only=True)
    is_device_activated = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'customer_code', 'first_name', 'last_name', 'name', 'full_name', 'email', 'mobile_number', 'alternate_mobile_number',
            'address', 'city', 'state', 'pincode', 
            'customer_photo', 'customer_signature',
            'id_type', 'id_number', 'masked_id_number', 'id_front_image', 'id_back_image',
            'signed_emi_agreement_flag', 'draft_status',
            'password', 'parent', 'role', 'is_active', 'date_joined',
            'device_summary', 'contract_summary', 'emi_status',
            'status', 'customer_status', 'can_edit', 'is_device_activated'
        ]
        read_only_fields = [
            'id', 'customer_code', 'date_joined', 'role', 'parent', 'is_active', 'masked_id_number',
            'device_summary', 'contract_summary', 'emi_status',
            'status', 'customer_status', 'can_edit', 'is_device_activated'
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

    def get_status(self, obj):
        from devices.models import DeviceLockStatus
        device = obj.registered_devices.first()
        if not device:
            return "INACTIVE"
        if device.status == DeviceLockStatus.UNENROLLED or device.provider_status == 'UNREGISTERED':
            return "UNINSTALL"
        if getattr(device, 'is_activated', False) or device.status in [DeviceLockStatus.ACTIVE, DeviceLockStatus.LOCKED]:
            return "ACTIVE"
        return "INACTIVE"

    def get_customer_status(self, obj):
        return self.get_status(obj)

    def get_is_device_activated(self, obj):
        from devices.models import DeviceLockStatus
        device = obj.registered_devices.first()
        if not device:
            return False
        if device.status == DeviceLockStatus.UNENROLLED or device.provider_status == 'UNREGISTERED':
            return False
        return bool(
            getattr(device, 'is_activated', False)
            or device.status in [DeviceLockStatus.ACTIVE, DeviceLockStatus.LOCKED]
        )

    def get_can_edit(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user and getattr(user, 'role', None) == 'SUPER_ADMIN':
            return True
        return not self.get_is_device_activated(obj)

    def get_device_summary(self, obj):
        device = obj.registered_devices.first()
        if not device:
            return None
        return {
            "id": device.id,
            "imei": device.imei,
            "secondary_imei": device.secondary_imei,
            "serial_number": device.serial_number,
            "brand": device.brand,
            "model": device.model,
            "device_name": device.device_name,
            "device_category": device.device_category,
            "status": device.status,
            "is_online": device.is_online,
            "is_activated": getattr(device, 'is_activated', False),
            "lock_provider": device.lock_provider,
            "provider_device_id": device.provider_device_id,
            "provider_status": device.provider_status,
            "qr_code_url": device.qr_code_url,
            "qr_codes": getattr(device, 'qr_codes', {}) or {},
            "qr_service_app": (getattr(device, 'qr_codes', {}) or {}).get('qr_service_app') or device.qr_code_url,
            "qr_running_device": (getattr(device, 'qr_codes', {}) or {}).get('qr_running_device'),
            "qr_test_dpc": (getattr(device, 'qr_codes', {}) or {}).get('qr_test_dpc'),
            "loan_provider": device.loan_provider,
            "loan_account_number": device.loan_account_number,
            "sim_operator": getattr(device, 'sim_operator', None),
            "sim_number": getattr(device, 'sim_number', None),
            "sim_serial": getattr(device, 'sim_serial', None),
            "sim_operator_2": getattr(device, 'sim_operator_2', None),
            "sim_number_2": getattr(device, 'sim_number_2', None)
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
        
        c_status = self.get_status(instance)
        representation['status'] = c_status
        representation['customer_status'] = c_status
            
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

def extract_login_identifier(attrs):
    identifier = (attrs.get('mobile_number') or attrs.get('username') or '').strip()
    if not identifier:
        raise serializers.ValidationError({
            "mobile_number": "Please provide a registered mobile number or username."
        })
    attrs['identifier'] = identifier
    return attrs


def validate_mpin_value(value, field_name='mpin'):
    mpin_str = str(value or '').strip()
    if not mpin_str.isdigit() or len(mpin_str) not in (4, 6):
        raise serializers.ValidationError({
            field_name: "MPIN must be exactly 4 or 6 numeric digits."
        })
    return mpin_str


class PasswordResetRequestSerializer(serializers.Serializer):
    # Accepts mobile number, username, or email
    username = serializers.CharField(required=False, allow_blank=True)
    mobile_number = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        return extract_login_identifier(attrs)


class PasswordResetVerifyOTPSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    mobile_number = serializers.CharField(required=False, allow_blank=True)
    otp = serializers.CharField(required=True, max_length=6)

    def validate(self, attrs):
        extract_login_identifier(attrs)
        attrs['otp'] = attrs['otp'].strip()
        return attrs


class PasswordResetConfirmSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    mobile_number = serializers.CharField(required=False, allow_blank=True)
    otp = serializers.CharField(required=True, max_length=6)
    new_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    confirm_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    def validate(self, attrs):
        extract_login_identifier(attrs)
        attrs['otp'] = attrs['otp'].strip()
        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError({"password": "Passwords must match."})
        return attrs


class MPINSetupSerializer(serializers.Serializer):
    mpin = serializers.CharField(write_only=True, required=True, min_length=4, max_length=6)
    confirm_mpin = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=6)

    def validate(self, attrs):
        mpin = validate_mpin_value(attrs.get('mpin'), 'mpin')
        confirm_mpin = attrs.get('confirm_mpin')
        if confirm_mpin is not None and str(confirm_mpin).strip() != '':
            if mpin != str(confirm_mpin).strip():
                raise serializers.ValidationError({"confirm_mpin": "MPIN and Confirm MPIN do not match."})
        attrs['mpin'] = mpin
        return attrs


class MPINLoginSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    mobile_number = serializers.CharField(required=False, allow_blank=True)
    mpin = serializers.CharField(write_only=True, required=True, min_length=4, max_length=6)
    keep_me_signed_in = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        extract_login_identifier(attrs)
        attrs['mpin'] = validate_mpin_value(attrs.get('mpin'), 'mpin')
        return attrs


class MPINForgotSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    mobile_number = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        return extract_login_identifier(attrs)


class MPINResetSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    mobile_number = serializers.CharField(required=False, allow_blank=True)
    otp = serializers.CharField(required=True, max_length=6)
    new_mpin = serializers.CharField(write_only=True, required=False, min_length=4, max_length=6)
    mpin = serializers.CharField(write_only=True, required=False, min_length=4, max_length=6)
    confirm_mpin = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=6)

    def validate(self, attrs):
        extract_login_identifier(attrs)
        attrs['otp'] = attrs['otp'].strip()
        raw_mpin = attrs.get('new_mpin') or attrs.get('mpin')
        new_mpin = validate_mpin_value(raw_mpin, 'new_mpin')
        confirm_mpin = attrs.get('confirm_mpin')
        if confirm_mpin is not None and str(confirm_mpin).strip() != '':
            if new_mpin != str(confirm_mpin).strip():
                raise serializers.ValidationError({"confirm_mpin": "New MPIN and Confirm MPIN do not match."})
        attrs['new_mpin'] = new_mpin
        return attrs

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


class RetailerBankAccountSubmitSerializer(serializers.Serializer):
    account_holder_name = serializers.CharField(max_length=150, required=True)
    bank_name = serializers.CharField(max_length=150, required=True)
    account_number = serializers.CharField(max_length=34, required=True)
    confirm_account_number = serializers.CharField(max_length=34, required=True, write_only=True)
    ifsc_code = serializers.CharField(max_length=11, required=True)
    upi_id = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    mobile_number = serializers.CharField(max_length=15, required=True)
    verify_now = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        import re
        holder = (attrs.get('account_holder_name') or '').strip()
        bank_name = (attrs.get('bank_name') or '').strip()
        acct = re.sub(r'\s+', '', str(attrs.get('account_number') or ''))
        confirm_acct = re.sub(r'\s+', '', str(attrs.get('confirm_account_number') or ''))
        ifsc = (attrs.get('ifsc_code') or '').strip().upper()
        upi = (attrs.get('upi_id') or '').strip().lower() or None
        mobile = (attrs.get('mobile_number') or '').strip()

        if not holder:
            raise serializers.ValidationError({'account_holder_name': 'Account holder name is required.'})
        if not bank_name:
            raise serializers.ValidationError({'bank_name': 'Bank name is required.'})
        if not acct.isdigit() or not (9 <= len(acct) <= 18):
            raise serializers.ValidationError({'account_number': 'Account number must contain 9 to 18 numeric digits.'})
        if acct != confirm_acct:
            raise serializers.ValidationError({'confirm_account_number': 'Account Number and Confirm Account Number do not match.'})
        if not re.match(r'^[A-Z]{4}0[A-Z0-9]{6}$', ifsc):
            raise serializers.ValidationError({'ifsc_code': 'Invalid IFSC code format (expected 11 characters, e.g. HDFC0001234).'})
        if upi and not re.match(r'^[a-z0-9._\-]{2,}@[a-z0-9.\-]{2,}$', upi):
            raise serializers.ValidationError({'upi_id': 'Invalid UPI ID format (e.g. store@okaxis).'})
        mobile_digits = ''.join(ch for ch in mobile if ch.isdigit())
        if len(mobile_digits) < 10:
            raise serializers.ValidationError({'mobile_number': 'Valid 10-digit mobile number is required.'})

        attrs['account_holder_name'] = holder
        attrs['bank_name'] = bank_name
        attrs['account_number'] = acct
        attrs.pop('confirm_account_number', None)
        attrs['ifsc_code'] = ifsc
        attrs['upi_id'] = upi
        attrs['mobile_number'] = mobile
        return attrs


class RetailerBankAccountOwnSerializer(serializers.ModelSerializer):
    account_number_masked = serializers.CharField(source='masked_account_number', read_only=True)
    upi_id_masked = serializers.CharField(source='masked_upi_id', read_only=True)
    retailer_id = serializers.IntegerField(source='retailer.id', read_only=True)
    retailer_code = serializers.SerializerMethodField(read_only=True)
    retailer_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        from .models import RetailerBankAccount
        model = RetailerBankAccount
        fields = [
            'id', 'retailer_id', 'retailer_code', 'retailer_name',
            'account_holder_name', 'bank_name',
            'account_number', 'account_number_masked',
            'ifsc_code', 'upi_id', 'upi_id_masked', 'mobile_number',
            'verification_status', 'verified_at', 'verification_ref', 'verification_remarks',
            'mandate_status', 'mandate_id', 'mandate_reference_id',
            'mandate_auth_url', 'mandate_registered_at',
            'last_transaction_status', 'last_transaction_at',
            'created_at', 'updated_at',
        ]

    def get_retailer_code(self, obj):
        return f'RET-{obj.retailer_id:04d}'

    def get_retailer_name(self, obj):
        r = obj.retailer
        return f"{r.first_name} {r.last_name}".strip() or r.business_name or r.username


class RetailerBankVerifyAdminSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['VERIFY', 'FAILED', 'REJECT'], required=False, default='VERIFY')
    use_gateway = serializers.BooleanField(required=False, default=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class RetailerMandateHistorySerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        from .models import RetailerMandateHistory
        model = RetailerMandateHistory
        fields = [
            'id', 'event_type', 'status', 'mandate_reference_id',
            'transaction_ref', 'amount', 'notes',
            'performed_by', 'performed_by_name', 'created_at',
        ]

    def get_performed_by_name(self, obj):
        if not obj.performed_by:
            return 'System / Webhook'
        u = obj.performed_by
        return f"{u.first_name} {u.last_name}".strip() or u.username

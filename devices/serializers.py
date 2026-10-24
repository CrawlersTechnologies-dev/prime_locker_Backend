from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import (
    Device,
    DeviceCommand,
    DeviceLocationLog,
    AppVersion,
    DeviceCategory,
    OSType,
    DeviceScannerState,
    DeviceLockStatus,
    CommandType,
    CommandStatus,
    AppType
)
from .imei_lookup import lookup_imei
from keys.models import DeviceKey, KeyStatus, KeyAction, KeyTransaction
from emi.models import CustomerEMIContract

User = get_user_model()


class DeviceSerializer(serializers.ModelSerializer):
    customer_details = serializers.SerializerMethodField(read_only=True)
    retailer_details = serializers.SerializerMethodField(read_only=True)
    is_online = serializers.ReadOnlyField()
    activation_key_string = serializers.CharField(write_only=True, required=False, allow_blank=True)
    emi_contract_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = Device
        fields = [
            'id',
            'customer',
            'customer_details',
            'retailer',
            'retailer_details',
            'device_category',
            'os_type',
            'device_state',
            'imei',
            'secondary_imei',
            'serial_number',
            'brand',
            'model',
            'device_name',
            'activation_key',
            'activation_key_string',
            'emi_contract',
            'emi_contract_id',
            'loan_provider',
            'loan_provider_other',
            'loan_account_number',
            'status',
            'is_online',
            'last_online_at',
            'ip_address',
            'app_version',
            'battery_level',
            # MDM Policy controls
            'block_factory_reset',
            'frp_protection_enabled',
            'camera_disabled',
            'social_media_disabled',
            'whatsapp_disabled',
            'instagram_disabled',
            'facebook_disabled',
            'wallpaper_locked',
            'custom_wallpaper',
            'custom_wallpaper_url',
            'lock_message',
            'offline_unlock_code',
            'offline_code_expires_at',
            'offline_code_expiry_type',
            'lock_provider',
            'provider_device_id',
            'provider_status',
            'streaming_blocked',
            'locked_packages',
            # Live GPS
            'current_latitude',
            'current_longitude',
            'current_address',
            'last_location_at',
            # Synced contacts & apps
            'synced_contacts',
            'installed_apps',
            'created_at',
            'updated_at'
        ]
        read_only_fields = [
            'id', 'retailer', 'activation_key', 'emi_contract', 'offline_unlock_code',
            'last_online_at', 'ip_address', 'last_location_at', 'created_at', 'updated_at'
        ]

    def get_customer_details(self, obj):
        if obj.customer:
            c = obj.customer
            name = f"{c.first_name} {c.last_name}".strip() or c.username
            request = self.context.get('request')
            photo_url = request.build_absolute_uri(c.customer_photo.url) if (request and c.customer_photo) else (c.customer_photo.url if c.customer_photo else None)
            sig_url = request.build_absolute_uri(c.customer_signature.url) if (request and c.customer_signature) else (c.customer_signature.url if c.customer_signature else None)
            return {
                'id': c.id,
                'name': name,
                'username': c.username,
                'mobile_number': c.mobile_number,
                'alternate_mobile_number': c.alternate_mobile_number,
                'customer_photo': photo_url,
                'customer_signature': sig_url
            }
        return None

    def get_retailer_details(self, obj):
        if obj.retailer:
            r = obj.retailer
            name = f"{r.first_name} {r.last_name}".strip() or r.username
            return {
                'id': r.id,
                'name': name,
                'business_name': r.business_name,
                'mobile_number': r.mobile_number,
                'city': r.city
            }
        return None

    def validate(self, attrs):
        imei = attrs.get('imei')
        # Auto-detect brand & model if omitted
        if imei and (not attrs.get('brand') or not attrs.get('model')):
            detected = lookup_imei(imei)
            if detected.get('is_valid'):
                if not attrs.get('brand'):
                    attrs['brand'] = detected['brand']
                if not attrs.get('model'):
                    attrs['model'] = detected['model']
                if not attrs.get('device_category'):
                    attrs['device_category'] = detected['device_category']
                if not attrs.get('os_type'):
                    attrs['os_type'] = detected['os_type']

        if not attrs.get('device_name'):
            brand = attrs.get('brand', '')
            model = attrs.get('model', '')
            attrs['device_name'] = f"{brand} {model}".strip() or "Device"

        return attrs

    def create(self, validated_data):
        key_string = validated_data.pop('activation_key_string', None)
        emi_id = validated_data.pop('emi_contract_id', None)
        request = self.context.get('request')

        # Link activation key if provided
        key_obj = None
        if key_string:
            clean_key = key_string.strip()
            key_obj = DeviceKey.objects.filter(key_string=clean_key).first()
            if not key_obj:
                raise serializers.ValidationError({"activation_key_string": f"Key '{clean_key}' is invalid."})
            if key_obj.status != KeyStatus.AVAILABLE:
                raise serializers.ValidationError({"activation_key_string": f"Key '{clean_key}' is already {key_obj.status}."})
            
            # Check ownership (unless Super Admin)
            if request and request.user.role != 'SUPER_ADMIN' and key_obj.current_owner != request.user:
                raise serializers.ValidationError({"activation_key_string": "You do not own this activation key."})

        # Link EMI contract if provided
        emi_obj = None
        if emi_id:
            emi_obj = CustomerEMIContract.objects.filter(id=emi_id).first()
            if not emi_obj:
                raise serializers.ValidationError({"emi_contract_id": f"EMI contract #{emi_id} not found."})

        device = Device.objects.create(
            activation_key=key_obj,
            emi_contract=emi_obj,
            **validated_data
        )

        # If key was supplied, activate it now
        if key_obj:
            key_obj.status = KeyStatus.ACTIVATED
            key_obj.activated_on_device_id = device.imei
            key_obj.activated_for_customer = device.customer
            key_obj.activated_at = timezone.now()
            key_obj.save()

            KeyTransaction.objects.create(
                key=key_obj,
                from_user=request.user if request else None,
                to_user=device.customer,
                action=KeyAction.ACTIVATED
            )

        return device


class DeviceCommandSerializer(serializers.ModelSerializer):
    issued_by_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DeviceCommand
        fields = [
            'id',
            'device',
            'command_type',
            'parameters',
            'status',
            'issued_by',
            'issued_by_name',
            'created_at',
            'executed_at',
            'error_message'
        ]
        read_only_fields = ['id', 'status', 'issued_by', 'created_at', 'executed_at', 'error_message']

    def get_issued_by_name(self, obj):
        if obj.issued_by:
            name = f"{obj.issued_by.first_name} {obj.issued_by.last_name}".strip()
            return name or obj.issued_by.username
        return "System"


class DeviceLocationLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceLocationLog
        fields = ['id', 'device', 'latitude', 'longitude', 'accuracy', 'address', 'recorded_at']
        read_only_fields = ['id', 'recorded_at']


class AppVersionSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.SerializerMethodField(read_only=True)
    download_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = AppVersion
        fields = [
            'id',
            'app_type',
            'version_name',
            'version_code',
            'apk_file',
            'download_url',
            'external_download_url',
            'release_notes',
            'is_mandatory',
            'is_active',
            'uploaded_by',
            'uploaded_by_name',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'uploaded_by', 'created_at', 'updated_at']

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            name = f"{obj.uploaded_by.first_name} {obj.uploaded_by.last_name}".strip()
            return name or obj.uploaded_by.username
        return None

    def get_download_url(self, obj):
        request = self.context.get('request')
        if obj.apk_file:
            if request:
                return request.build_absolute_uri(obj.apk_file.url)
            return obj.apk_file.url
        return obj.external_download_url or None


class DeviceHeartbeatSerializer(serializers.Serializer):
    """
    Heartbeat ping payload sent silently by the Android Locker APK or TV App.
    """
    imei = serializers.CharField(max_length=50, required=True)
    battery_level = serializers.IntegerField(required=False, min_value=0, max_value=100)
    app_version = serializers.CharField(max_length=50, required=False, allow_blank=True)
    latitude = serializers.DecimalField(max_digits=10, decimal_places=7, required=False)
    longitude = serializers.DecimalField(max_digits=10, decimal_places=7, required=False)
    address = serializers.CharField(required=False, allow_blank=True)
    synced_contacts = serializers.ListField(required=False, child=serializers.DictField())


class LockMessageTemplateSerializer(serializers.ModelSerializer):
    """
    Serializer for Screen A-40 Lock Message Templates.
    """
    class Meta:
        from .models import LockMessageTemplate
        model = LockMessageTemplate
        fields = ['id', 'title', 'text', 'is_default', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

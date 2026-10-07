from rest_framework import serializers
from django.contrib.auth import get_user_model
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
    
    # We don't require 'username' in the request, we will generate it from email
    username = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'mobile_number', 'full_name', 'business_name',
            'pan_number', 'gst_number', 'state', 'city', 'pincode', 'address',
            'kyc_document', 'remarks', 'can_reclaim_keys', 'password', 'confirm_password',
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

    def create(self, validated_data):
        validated_data.pop('confirm_password')
        password = validated_data.pop('password')
        full_name = validated_data.pop('full_name')
        
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

class DistributorSerializer(SuperDistributorSerializer):
    """Inherits identical fields from SuperDistributorSerializer"""
    pass

class SubDistributorSerializer(SuperDistributorSerializer):
    """Inherits identical fields from SuperDistributorSerializer"""
    pass

class RetailerSerializer(SuperDistributorSerializer):
    """Retailer uses similar fields (Store Name -> business_name)"""
    pass

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


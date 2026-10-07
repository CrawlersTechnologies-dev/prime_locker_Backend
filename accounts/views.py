from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from .serializers import (
    CustomTokenObtainPairSerializer, 
    UserSerializer, 
    SuperDistributorSerializer,
    DistributorSerializer,
    SubDistributorSerializer,
    RetailerSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer
)
from .models import AuditLog, PasswordResetOTP
import random

User = get_user_model()

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0]
    return request.META.get('REMOTE_ADDR')

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            # Login successful, create audit log
            # The serializer valid method can be used to get the user, but we can also get it from username
            username = request.data.get('username')
            user = User.objects.filter(username=username).first() or \
                   User.objects.filter(email=username).first() or \
                   User.objects.filter(mobile_number=username).first()
            if user:
                AuditLog.objects.create(
                    user=user,
                    action="USER_LOGIN",
                    ip_address=get_client_ip(request),
                    details={"method": "password"}
                )
        return response

from .permissions import RoleHierarchyPermission

class BaseRoleViewSet(viewsets.ModelViewSet):
    """Base ViewSet to handle common logic for all role-based user management."""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, RoleHierarchyPermission]
    target_role = None  # To be overridden by subclasses

    def get_queryset(self):
        user = self.request.user
        queryset = User.objects.filter(role=self.target_role)
        
        if user.role == 'SUPER_ADMIN':
            return queryset
            
        return queryset.filter(parent=user)

    def perform_create(self, serializer):
        user = self.request.user
        parent = self.request.data.get('parent')
        
        if user.role == 'SUPER_ADMIN' and parent:
            parent_instance = User.objects.filter(id=parent).first()
            serializer.save(parent=parent_instance, role=self.target_role)
        else:
            serializer.save(parent=user, role=self.target_role)
            
        AuditLog.objects.create(
            user=user,
            action=f"CREATED_{self.target_role}",
            ip_address=get_client_ip(self.request),
            details={"created_username": serializer.instance.username}
        )

    def perform_update(self, serializer):
        user = self.request.user
        parent = self.request.data.get('parent')
        
        # Only SUPER_ADMIN can reassign parents
        if user.role == 'SUPER_ADMIN' and parent:
            parent_instance = User.objects.filter(id=parent).first()
            if parent_instance:
                serializer.save(parent=parent_instance)
            else:
                serializer.save()
        else:
            # If not SUPER_ADMIN, completely ignore any parent field sent in request
            # and just save the other fields
            serializer.save()
            
        AuditLog.objects.create(
            user=user,
            action=f"UPDATED_{self.target_role}",
            ip_address=get_client_ip(self.request),
            details={"updated_username": serializer.instance.username}
        )

    def destroy(self, request, *args, **kwargs):
        if request.user.role != 'SUPER_ADMIN':
            return Response(
                {"detail": "Only Super Admin can delete users."},
                status=status.HTTP_403_FORBIDDEN
            )
            
        instance = self.get_object()
        username = instance.username
        
        response = super().destroy(request, *args, **kwargs)
        
        AuditLog.objects.create(
            user=request.user,
            action=f"DELETED_{self.target_role}",
            ip_address=get_client_ip(self.request),
            details={"deleted_username": username}
        )
        return response

class SuperDistributorViewSet(BaseRoleViewSet):
    target_role = 'SUPER_DISTRIBUTOR'
    serializer_class = SuperDistributorSerializer

class DistributorViewSet(BaseRoleViewSet):
    target_role = 'DISTRIBUTOR'
    serializer_class = DistributorSerializer

class SubDistributorViewSet(BaseRoleViewSet):
    target_role = 'SUB_DISTRIBUTOR'
    serializer_class = SubDistributorSerializer

class RetailerViewSet(BaseRoleViewSet):
    target_role = 'RETAILER'
    serializer_class = RetailerSerializer

class CustomerViewSet(BaseRoleViewSet):
    target_role = 'CUSTOMER'

class PasswordResetRequestView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            user = User.objects.filter(username=username).first() or \
                   User.objects.filter(email=username).first() or \
                   User.objects.filter(mobile_number=username).first()
            
            if user:
                # Generate 6 digit OTP
                otp_code = str(random.randint(100000, 999999))
                PasswordResetOTP.objects.create(user=user, otp=otp_code)
                
                # TODO: Integrate real Email/SMS provider here
                print(f"--- OTP for {user.username} is {otp_code} ---")
                
                return Response({
                    "message": "If the account exists, an OTP has been sent."
                }, status=status.HTTP_200_OK)
            return Response({"message": "If the account exists, an OTP has been sent."}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            otp_code = serializer.validated_data['otp']
            new_password = serializer.validated_data['new_password']

            user = User.objects.filter(username=username).first() or \
                   User.objects.filter(email=username).first() or \
                   User.objects.filter(mobile_number=username).first()

            if not user:
                return Response({"error": "Invalid details"}, status=status.HTTP_400_BAD_REQUEST)

            otp_record = PasswordResetOTP.objects.filter(user=user, otp=otp_code).order_by('-created_at').first()
            
            if otp_record and otp_record.is_valid():
                user.set_password(new_password)
                user.save()
                otp_record.is_used = True
                otp_record.save()
                
                AuditLog.objects.create(
                    user=user,
                    action="PASSWORD_RESET",
                    ip_address=get_client_ip(request)
                )
                
                return Response({"message": "Password reset successful."}, status=status.HTTP_200_OK)
            
            return Response({"error": "Invalid or expired OTP"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

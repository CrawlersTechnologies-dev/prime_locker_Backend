from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    CustomTokenObtainPairView,
    SuperDistributorViewSet,
    DistributorViewSet,
    SubDistributorViewSet,
    RetailerViewSet,
    CustomerViewSet,
    PasswordResetRequestView,
    PasswordResetConfirmView,
    DashboardView,
    AuditLogViewSet,
    CurrentUserView,
    LogoutView,
    ChangePasswordView,
    NotificationViewSet,
    StaffViewSet,
    PlatformSettingsView,
    WhatsAppCampaignView
)

router = DefaultRouter()
router.register(r'super-distributors', SuperDistributorViewSet, basename='super-distributor')
router.register(r'distributors', DistributorViewSet, basename='distributor')
router.register(r'sub-distributors', SubDistributorViewSet, basename='sub-distributor')
router.register(r'retailers', RetailerViewSet, basename='retailer')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-logs')
router.register(r'notifications', NotificationViewSet, basename='notifications')
router.register(r'staff', StaffViewSet, basename='staff')

urlpatterns = [
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/me/', CurrentUserView.as_view(), name='current_user'),
    path('auth/logout/', LogoutView.as_view(), name='auth_logout'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='auth_change_password'),
    path('auth/password-reset-request/', PasswordResetRequestView.as_view(), name='password_reset_request'),
    path('auth/password-reset-confirm/', PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('dashboard/', DashboardView.as_view(), name='main-dashboard'),
    path('settings/', PlatformSettingsView.as_view(), name='platform_settings'),
    path('whatsapp/campaigns/', WhatsAppCampaignView.as_view(), name='whatsapp_campaigns'),
    path('', include(router.urls)),
]

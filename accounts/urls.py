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
    AuditLogViewSet
)

router = DefaultRouter()
router.register(r'super-distributors', SuperDistributorViewSet, basename='super-distributor')
router.register(r'distributors', DistributorViewSet, basename='distributor')
router.register(r'sub-distributors', SubDistributorViewSet, basename='sub-distributor')
router.register(r'retailers', RetailerViewSet, basename='retailer')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-logs')

urlpatterns = [
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/password-reset-request/', PasswordResetRequestView.as_view(), name='password_reset_request'),
    path('auth/password-reset-confirm/', PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('dashboard/', DashboardView.as_view(), name='main-dashboard'),
    path('', include(router.urls)),
]

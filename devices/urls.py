from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DeviceViewSet,
    DeviceHeartbeatView,
    AppVersionViewSet,
    PublicAppDownloadView,
    LockMessageTemplateViewSet,
    RocketPayWebhookView,
    AppleMDMWebhookView,
    ApprovedDeviceTACViewSet,
    InstallationVideoViewSet,
    RetailerEnrollmentQRsView
)

router = DefaultRouter()
router.register(r'devices', DeviceViewSet, basename='devices')
router.register(r'app-versions', AppVersionViewSet, basename='app-versions')
router.register(r'lock-messages', LockMessageTemplateViewSet, basename='lock-messages')
router.register(r'tac-list', ApprovedDeviceTACViewSet, basename='tac-list')
router.register(r'installation-videos', InstallationVideoViewSet, basename='installation-videos')

urlpatterns = [
    path('mdm/enrollment-qrs/', RetailerEnrollmentQRsView.as_view(), name='retailer-mdm-enrollment-qrs'),
    path('app-versions/download/<str:slug>/', PublicAppDownloadView.as_view(), name='public_app_download'),
    path('devices/ping/', DeviceHeartbeatView.as_view(), name='device-heartbeat'),
    path('devices/check-in/', DeviceHeartbeatView.as_view(), name='device-checkin-alias'),
    path('devices/heartbeat/', DeviceHeartbeatView.as_view(), name='device-heartbeat-alias'),
    path('devices/rocketpay/webhook/', RocketPayWebhookView.as_view(), name='rocketpay-webhook-alias'),
    path('integrations/rocketpay/webhook/', RocketPayWebhookView.as_view(), name='rocketpay-webhook'),
    path('devices/apple-mdm/webhook/', AppleMDMWebhookView.as_view(), name='apple-mdm-webhook-alias'),
    path('integrations/apple-mdm/webhook/', AppleMDMWebhookView.as_view(), name='apple-mdm-webhook'),
    path('', include(router.urls)),
]

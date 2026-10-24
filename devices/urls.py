from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DeviceViewSet,
    DeviceHeartbeatView,
    AppVersionViewSet,
    LockMessageTemplateViewSet,
    RocketPayWebhookView
)

router = DefaultRouter()
router.register(r'devices', DeviceViewSet, basename='devices')
router.register(r'app-versions', AppVersionViewSet, basename='app-versions')
router.register(r'lock-messages', LockMessageTemplateViewSet, basename='lock-messages')

urlpatterns = [
    path('devices/ping/', DeviceHeartbeatView.as_view(), name='device-heartbeat'),
    path('devices/check-in/', DeviceHeartbeatView.as_view(), name='device-checkin-alias'),
    path('devices/rocketpay/webhook/', RocketPayWebhookView.as_view(), name='rocketpay-webhook-alias'),
    path('integrations/rocketpay/webhook/', RocketPayWebhookView.as_view(), name='rocketpay-webhook'),
    path('', include(router.urls)),
]

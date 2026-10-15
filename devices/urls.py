from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DeviceViewSet,
    DeviceHeartbeatView,
    AppVersionViewSet
)

router = DefaultRouter()
router.register(r'devices', DeviceViewSet, basename='devices')
router.register(r'app-versions', AppVersionViewSet, basename='app-versions')

urlpatterns = [
    path('devices/ping/', DeviceHeartbeatView.as_view(), name='device-heartbeat'),
    path('', include(router.urls)),
]

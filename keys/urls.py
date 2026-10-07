from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import KeyViewSet, KeyRequestViewSet

router = DefaultRouter()
router.register(r'keys', KeyViewSet, basename='keys')
router.register(r'key-requests', KeyRequestViewSet, basename='key-requests')

urlpatterns = [
    path('', include(router.urls)),
]

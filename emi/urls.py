from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EMIPlanTemplateViewSet,
    CustomerEMIContractViewSet,
    EMIInstallmentViewSet,
    OverdueEMIsViewSet,
    RazorpayWebhookView,
    PaymentViewSet
)

router = DefaultRouter()
router.register(r'emi-plans', EMIPlanTemplateViewSet, basename='emi-plans')
router.register(r'emi-contracts', CustomerEMIContractViewSet, basename='emi-contracts')
router.register(r'emi-installments', EMIInstallmentViewSet, basename='emi-installments')
router.register(r'emi-overdue', OverdueEMIsViewSet, basename='emi-overdue')
router.register(r'payments', PaymentViewSet, basename='payments')

urlpatterns = [
    path('', include(router.urls)),
    path('emi/webhook/razorpay/', RazorpayWebhookView.as_view(), name='razorpay-webhook'),
]

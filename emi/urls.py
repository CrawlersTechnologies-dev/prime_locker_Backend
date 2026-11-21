from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EMIPlanTemplateViewSet,
    CustomerEMIContractViewSet,
    EMIInstallmentViewSet,
    OverdueEMIsViewSet,
    CashfreeWebhookView,
    CashfreeMandateCheckoutView,
    PaymentViewSet,
    RetailerWalletViewSet,
    PayoutRequestViewSet,
    RevenueDashboardViewSet
)

router = DefaultRouter()
router.register(r'emi-plans', EMIPlanTemplateViewSet, basename='emi-plans')
router.register(r'emi-contracts', CustomerEMIContractViewSet, basename='emi-contracts')
router.register(r'emi-installments', EMIInstallmentViewSet, basename='emi-installments')
router.register(r'emi-overdue', OverdueEMIsViewSet, basename='emi-overdue')
router.register(r'payments', PaymentViewSet, basename='payments')
router.register(r'wallet', RetailerWalletViewSet, basename='wallet')
router.register(r'payouts', PayoutRequestViewSet, basename='payouts')
router.register(r'revenue', RevenueDashboardViewSet, basename='revenue')

urlpatterns = [
    path('', include(router.urls)),
    path('emi/webhook/cashfree/', CashfreeWebhookView.as_view(), name='cashfree-webhook'),
    path('emi/mandate/checkout/<str:mandate_id>/', CashfreeMandateCheckoutView.as_view(), name='cashfree-mandate-checkout'),
]

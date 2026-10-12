from django.urls import path
from .views import (
    CollectionsSummaryReportView,
    OverdueAgeingReportView,
    RetailerPerformanceReportView,
    PaymentsByModeReportView,
    LocksUnlocksReportView,
    CustomerKYCReportView
)

urlpatterns = [
    path('collections-summary/', CollectionsSummaryReportView.as_view(), name='report-collections-summary'),
    path('overdue-ageing/', OverdueAgeingReportView.as_view(), name='report-overdue-ageing'),
    path('retailer-performance/', RetailerPerformanceReportView.as_view(), name='report-retailer-performance'),
    path('payments-by-mode/', PaymentsByModeReportView.as_view(), name='report-payments-by-mode'),
    path('locks-unlocks/', LocksUnlocksReportView.as_view(), name='report-locks-unlocks'),
    path('customer-kyc/', CustomerKYCReportView.as_view(), name='report-customer-kyc'),
]

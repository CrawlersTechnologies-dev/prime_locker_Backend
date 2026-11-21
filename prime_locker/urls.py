"""
URL configuration for prime_locker project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from accounts.views import ProtectedMediaView, DeviceActivateAuthView
from devices.views import PublicAppDownloadView, RetailerEnrollmentQRsView
from emi.views import CashfreeMandateCheckoutView

urlpatterns = [
    path('mdm/enrollment-qrs/', RetailerEnrollmentQRsView.as_view(), name='root_retailer_mdm_enrollment_qrs'),
    path('app-versions/download/<str:slug>/', PublicAppDownloadView.as_view(), name='root_public_app_download'),
    path('auth/device-activate/', DeviceActivateAuthView.as_view(), name='root_device_activate_auth'),
    path('admin/', admin.site.urls),
    path('media/<path:file_path>', ProtectedMediaView.as_view(), name='protected-media'),
    path('emi/mandate/checkout/<str:mandate_id>/', CashfreeMandateCheckoutView.as_view(), name='root_cashfree_mandate_checkout'),
    path('api/', include('accounts.urls')),
    path('api/', include('keys.urls')),
    path('api/', include('emi.urls')),
    path('api/', include('offers.urls')),
    path('api/reports/', include('reports.urls')),
    path('api/', include('devices.urls')),
]

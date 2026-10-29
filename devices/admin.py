from django.contrib import admin
from .models import (
    Device,
    DeviceCommand,
    DeviceLocationLog,
    AppVersion,
    LockMessageTemplate,
    ApprovedDeviceTAC,
    InstallationVideo
)


@admin.register(InstallationVideo)
class InstallationVideoAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'source_type', 'target_role', 'display_order', 'is_active', 'created_at')
    list_filter = ('category', 'source_type', 'target_role', 'is_active')
    search_fields = ('title', 'description')
    ordering = ('display_order', '-created_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ('imei', 'brand', 'model', 'customer', 'retailer', 'status', 'lock_provider', 'created_at')
    list_filter = ('status', 'lock_provider', 'device_category', 'os_type')
    search_fields = ('imei', 'secondary_imei', 'brand', 'model')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AppVersion)
class AppVersionAdmin(admin.ModelAdmin):
    list_display = ('app_type', 'version_name', 'version_code', 'is_active', 'is_mandatory', 'created_at')
    list_filter = ('app_type', 'is_active', 'is_mandatory')
    ordering = ('-version_code',)


@admin.register(LockMessageTemplate)
class LockMessageTemplateAdmin(admin.ModelAdmin):
    list_display = ('title', 'is_default', 'created_by', 'created_at')
    list_filter = ('is_default',)


@admin.register(ApprovedDeviceTAC)
class ApprovedDeviceTACAdmin(admin.ModelAdmin):
    list_display = ('tac', 'brand', 'model_number', 'marketing_name', 'environment', 'active')
    list_filter = ('environment', 'active', 'brand')
    search_fields = ('tac', 'brand', 'model_number', 'marketing_name')

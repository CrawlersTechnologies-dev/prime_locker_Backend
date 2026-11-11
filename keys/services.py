from django.db.models import Q
from .models import DeviceKey, KeyStatus

ZERO_KEY_MESSAGE = (
    "You don't have any available keys for this service. "
    "Please contact your distributor to request additional keys."
)

SERVICE_META = {
    'ANDROID_MDM': {
        'label': 'Android MDM',
        'code': 'ANDROID_MDM',
        'device_type': 'ANDROID',
        'filter': (
            (Q(key_string__istartswith='AND-') | Q(device_type__iexact='ANDROID'))
            & ~Q(key_string__istartswith='DLC-')
            & ~Q(key_string__istartswith='IOS-')
            & ~Q(key_string__istartswith='TV-')
        ),
    },
    'ANDROID_DLC': {
        'label': 'Android DLC Locker',
        'code': 'ANDROID_DLC',
        'device_type': 'DLC',
        'filter': Q(key_string__istartswith='DLC-') | Q(device_type__iexact='DLC'),
    },
    'IOS_LOCKER': {
        'label': 'iOS Locker',
        'code': 'IOS_LOCKER',
        'device_type': 'IOS',
        'filter': Q(key_string__istartswith='IOS-') | Q(device_type__iexact='IOS'),
    },
    'SMART_TV': {
        'label': 'Smart TV Locker',
        'code': 'SMART_TV',
        'device_type': 'TV',
        'filter': Q(key_string__istartswith='TV-') | Q(device_type__iexact='TV'),
    },
}

SERVICE_ALIASES = {
    'ANDROID_MDM': 'ANDROID_MDM',
    'ANDROID': 'ANDROID_MDM',
    'MOBILE_ANDROID': 'ANDROID_MDM',
    'MDM': 'ANDROID_MDM',
    'PARTNER_MDM': 'ANDROID_MDM',
    'DPC': 'ANDROID_MDM',
    'ANDROID_DLC': 'ANDROID_DLC',
    'DLC': 'ANDROID_DLC',
    'DLC_LOCKER': 'ANDROID_DLC',
    'ANDROID_DLC_LOCKER': 'ANDROID_DLC',
    'ROCKETPAY': 'ANDROID_DLC',
    'IOS_LOCKER': 'IOS_LOCKER',
    'IOS': 'IOS_LOCKER',
    'IPHONE': 'IOS_LOCKER',
    'MOBILE_IOS': 'IOS_LOCKER',
    'APPLE_MDM': 'IOS_LOCKER',
    'IOS_AGENT': 'IOS_LOCKER',
    'SMART_TV': 'SMART_TV',
    'TV': 'SMART_TV',
}


def resolve_service_code(service=None, mdm_type=None, lock_provider=None, os_type=None, device_category=None):
    """Maps incoming service, MDM type, lock provider, or OS hints to a canonical service code."""
    for candidate in (service, mdm_type, lock_provider, os_type, device_category):
        if candidate:
            norm = str(candidate).strip().upper().replace(' ', '_').replace('-', '_')
            if norm in SERVICE_ALIASES:
                return SERVICE_ALIASES[norm]
    return 'ANDROID_MDM'


def get_service_key_queryset(user, service_code):
    """Returns AVAILABLE keys owned by user matching the canonical service code."""
    meta = SERVICE_META.get(service_code, SERVICE_META['ANDROID_MDM'])
    return DeviceKey.objects.filter(
        current_owner=user,
        status=KeyStatus.AVAILABLE,
    ).filter(meta['filter'])


def get_service_balance_info(user, service_code):
    """Returns balance summary for the requested service code."""
    meta = SERVICE_META.get(service_code, SERVICE_META['ANDROID_MDM'])
    balance = get_service_key_queryset(user, meta['code']).count()
    return {
        'service': meta['label'],
        'service_code': meta['code'],
        'device_type': meta['device_type'],
        'available_balance': balance,
        'can_enroll': balance > 0,
        'message': None if balance > 0 else ZERO_KEY_MESSAGE,
    }

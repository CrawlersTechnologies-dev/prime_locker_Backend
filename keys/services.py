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
            (
                Q(key_string__istartswith='AND-')
                | Q(key_string__istartswith='MOB-')
                | Q(device_type__iexact='ANDROID')
                | Q(device_type__iexact='MOBILE')
            )
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
        'available_key_count': balance,
        'available_keys_count': balance,
        'total_available': balance,
        'total_available_keys': balance,
        'count': balance,
        'key_count': balance,
        'total_keys': balance,
    }


def get_user_available_keys_breakdown(user):
    """
    Returns structured breakdown of available keys for any non-customer user:
      - total: sum of all available keys
      - mdm_keys: Android Enterprise MDM / 6-tap / DPC keys
      - ios_keys: Apple iOS MDM keys
      - dlc_keys: Android DLC / RocketPay OEM Knox keys
      - tv_keys: Smart TV keys
    Returns None for CUSTOMER users.
    """
    if not user or getattr(user, 'role', None) == 'CUSTOMER':
        return None

    from django.db.models import Count, Case, When, IntegerField
    from .models import DeviceKey, KeyStatus

    avail_qs = DeviceKey.objects.filter(current_owner=user, status=KeyStatus.AVAILABLE)

    aggs = avail_qs.aggregate(
        mdm=Count(Case(When(SERVICE_META['ANDROID_MDM']['filter'], then=1), output_field=IntegerField())),
        dlc=Count(Case(When(SERVICE_META['ANDROID_DLC']['filter'], then=1), output_field=IntegerField())),
        ios=Count(Case(When(SERVICE_META['IOS_LOCKER']['filter'], then=1), output_field=IntegerField())),
        tv=Count(Case(When(SERVICE_META['SMART_TV']['filter'], then=1), output_field=IntegerField())),
        total=Count('id'),
    )

    mdm_count = aggs.get('mdm') or 0
    dlc_count = aggs.get('dlc') or 0
    ios_count = aggs.get('ios') or 0
    tv_count = aggs.get('tv') or 0
    total_count = aggs.get('total') or 0

    return {
        'total': total_count,
        'count': total_count,
        'available_keys': total_count,
        'available_key_count': total_count,
        'available_keys_count': total_count,
        'total_available_keys': total_count,
        'total_keys': total_count,
        'keys_available': total_count,
        'key_count': total_count,
        'mdm_keys': mdm_count,
        'android_keys': mdm_count,
        'dpc_keys': mdm_count,
        'ios_keys': ios_count,
        'dlc_keys': dlc_count,
        'tv_keys': tv_count,
        'ANDROID': mdm_count,
        'IOS': ios_count,
        'DLC': dlc_count,
        'TV': tv_count,
    }


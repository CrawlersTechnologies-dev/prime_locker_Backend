"""
IMEI TAC (Type Allocation Code) Lookup Engine.
Auto-detects Brand, Model, Category, and OS Type from the 15-digit IMEI number.
"""

# Common TAC database mapping (First 8 digits of IMEI)
KNOWN_TACS = {
    # Apple iPhones (iOS)
    '35205511': {'brand': 'Apple', 'model': 'iPhone 15 Pro Max', 'category': 'MOBILE', 'os': 'IOS'},
    '35205411': {'brand': 'Apple', 'model': 'iPhone 15 Pro', 'category': 'MOBILE', 'os': 'IOS'},
    '35205211': {'brand': 'Apple', 'model': 'iPhone 15', 'category': 'MOBILE', 'os': 'IOS'},
    '35304610': {'brand': 'Apple', 'model': 'iPhone 14 Pro Max', 'category': 'MOBILE', 'os': 'IOS'},
    '35304510': {'brand': 'Apple', 'model': 'iPhone 14', 'category': 'MOBILE', 'os': 'IOS'},
    '35678211': {'brand': 'Apple', 'model': 'iPhone 13 Pro', 'category': 'MOBILE', 'os': 'IOS'},
    '35678011': {'brand': 'Apple', 'model': 'iPhone 13', 'category': 'MOBILE', 'os': 'IOS'},
    '35456711': {'brand': 'Apple', 'model': 'iPhone 12', 'category': 'MOBILE', 'os': 'IOS'},

    # Samsung Galaxy (Android)
    '35891022': {'brand': 'Samsung', 'model': 'Galaxy S23 Ultra', 'category': 'MOBILE', 'os': 'ANDROID'},
    '35891021': {'brand': 'Samsung', 'model': 'Galaxy S23', 'category': 'MOBILE', 'os': 'ANDROID'},
    '35789211': {'brand': 'Samsung', 'model': 'Galaxy S22 5G', 'category': 'MOBILE', 'os': 'ANDROID'},
    '35442111': {'brand': 'Samsung', 'model': 'Galaxy A54 5G', 'category': 'MOBILE', 'os': 'ANDROID'},
    '35442011': {'brand': 'Samsung', 'model': 'Galaxy A34 5G', 'category': 'MOBILE', 'os': 'ANDROID'},
    '35661211': {'brand': 'Samsung', 'model': 'Galaxy M34 5G', 'category': 'MOBILE', 'os': 'ANDROID'},
    '35991011': {'brand': 'Samsung', 'model': 'Galaxy Z Fold 5', 'category': 'MOBILE', 'os': 'ANDROID'},

    # Xiaomi / Redmi / POCO
    '86789105': {'brand': 'Xiaomi', 'model': 'Redmi Note 13 Pro', 'category': 'MOBILE', 'os': 'ANDROID'},
    '86789005': {'brand': 'Xiaomi', 'model': 'Redmi Note 12', 'category': 'MOBILE', 'os': 'ANDROID'},
    '86543205': {'brand': 'POCO', 'model': 'POCO X6 Pro 5G', 'category': 'MOBILE', 'os': 'ANDROID'},

    # OnePlus / Oppo / Realme
    '86123405': {'brand': 'OnePlus', 'model': 'OnePlus 12R', 'category': 'MOBILE', 'os': 'ANDROID'},
    '86123305': {'brand': 'OnePlus', 'model': 'OnePlus Nord CE 3', 'category': 'MOBILE', 'os': 'ANDROID'},
    '86987605': {'brand': 'Realme', 'model': 'Realme 12 Pro+ 5G', 'category': 'MOBILE', 'os': 'ANDROID'},
    '86987505': {'brand': 'Realme', 'model': 'Realme Narzo 60', 'category': 'MOBILE', 'os': 'ANDROID'},
    '86345605': {'brand': 'Vivo', 'model': 'Vivo V29 5G', 'category': 'MOBILE', 'os': 'ANDROID'},

    # Smart TVs / Connected Screens
    '99001234': {'brand': 'Samsung', 'model': 'Crystal 4K UHD Smart TV', 'category': 'TV', 'os': 'SMART_TV'},
    '99005678': {'brand': 'LG', 'model': 'OLED 4K Smart TV', 'category': 'TV', 'os': 'SMART_TV'},
    '99009900': {'brand': 'Xiaomi', 'model': 'Mi TV 5X 50 inch', 'category': 'TV', 'os': 'SMART_TV'},
    '99008800': {'brand': 'OnePlus', 'model': 'OnePlus TV Y1S Pro', 'category': 'TV', 'os': 'SMART_TV'},
}

# Manufacturer Prefix ranges
MANUFACTURER_RANGES = [
    ('35', 'Apple / Samsung / Global Brands'),
    ('86', 'Xiaomi / OnePlus / Oppo / Vivo / Realme'),
    ('01', 'Apple / North American Models'),
    ('99', 'Smart TVs & Connected IoT Devices'),
]

def lookup_imei(imei_string: str) -> dict:
    """
    Cleans IMEI and extracts TAC to lookup hardware brand, model, and OS.
    """
    clean_imei = str(imei_string).strip().replace('-', '').replace(' ', '')
    if len(clean_imei) < 8:
        return {
            "is_valid": False,
            "error": "IMEI must be at least 8 digits."
        }

    tac = clean_imei[:8]
    prefix = clean_imei[:2]

    # Exact TAC match
    if tac in KNOWN_TACS:
        info = KNOWN_TACS[tac]
        return {
            "is_valid": True,
            "is_identified": True,
            "imei": clean_imei,
            "tac": tac,
            "brand": info['brand'],
            "model": info['model'],
            "device_category": info['category'],
            "os_type": info['os'],
            "confidence": "HIGH"
        }

    # Range based heuristic deduction
    inferred_brand = "Android Device"
    inferred_category = "MOBILE"
    inferred_os = "ANDROID"

    if prefix == '01':
        inferred_brand = "Apple"
        inferred_os = "IOS"
    elif prefix == '35':
        inferred_brand = "Samsung / Android"
        inferred_os = "ANDROID"
    elif prefix == '86':
        inferred_brand = "Xiaomi / OnePlus / Realme"
        inferred_os = "ANDROID"
    elif prefix == '99':
        inferred_brand = "Smart TV"
        inferred_category = "TV"
        inferred_os = "SMART_TV"

    return {
        "is_valid": True,
        "is_identified": False,
        "imei": clean_imei,
        "tac": tac,
        "brand": inferred_brand,
        "model": f"Model TAC-{tac}",
        "device_category": inferred_category,
        "os_type": inferred_os,
        "confidence": "HEURISTIC"
    }

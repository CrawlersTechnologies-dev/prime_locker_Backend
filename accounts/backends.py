from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()

class EmailOrMobileBackend(ModelBackend):
    """
    Custom authentication backend supporting login via Username, Email, or Mobile Number.
    Handles duplicate mobile numbers gracefully without raising MultipleObjectsReturned
    or causing 500 server errors.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get('username') or kwargs.get('email')
        
        if not username or not password:
            return None

        query = str(username).strip()

        # 1. Exact match on username or email takes precedence
        exact_match = User.objects.filter(
            Q(username=query) | Q(email__iexact=query)
        ).first()
        if exact_match and exact_match.check_password(password) and self.user_can_authenticate(exact_match):
            return exact_match

        # 2. Match by mobile number (including handling with or without +91)
        mobile_filter = Q(mobile_number=query)
        if query.startswith('+91'):
            mobile_filter |= Q(mobile_number=query[3:])
        elif len(query) == 10 and query.isdigit():
            mobile_filter |= Q(mobile_number=f"+91{query}")

        candidates = User.objects.filter(
            Q(username=query) | Q(email__iexact=query) | mobile_filter
        ).order_by('-is_active', '-date_joined')

        for candidate in candidates:
            if candidate.check_password(password) and self.user_can_authenticate(candidate):
                return candidate

        return None

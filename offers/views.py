from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from django.utils import timezone

from .models import Offer, TargetRole
from .serializers import OfferSerializer
from accounts.models import AuditLog
from accounts.views import get_client_ip


class IsSuperAdminOrReadOnly(permissions.BasePermission):
    """
    Super Admin can perform any action (create, edit, delete).
    Other authenticated users have read-only access to offers meant for them.
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.role == 'SUPER_ADMIN'


class OfferViewSet(viewsets.ModelViewSet):
    """
    Offers & Promotions API.
    - Super-Admin: Creates and manages all offers.
    - Other Roles: See only active, valid offers targeted to their specific role or ALL.
    """
    permission_classes = [IsSuperAdminOrReadOnly]
    serializer_class = OfferSerializer

    def get_queryset(self):
        user = self.request.user
        now = timezone.now()

        # Super-Admin sees all offers
        if user.role == 'SUPER_ADMIN':
            qs = Offer.objects.all()
            target_role = self.request.query_params.get('target_role')
            if target_role:
                qs = qs.filter(target_role=target_role.upper())
            return qs

        # Other users: See only active and valid offers targeted to their role or ALL
        return Offer.objects.filter(
            Q(target_role=TargetRole.ALL) | Q(target_role=user.role),
            is_active=True,
            valid_from__lte=now
        ).filter(
            Q(valid_to__isnull=True) | Q(valid_to__gte=now)
        )

    def perform_create(self, serializer):
        instance = serializer.save(created_by=self.request.user)
        AuditLog.objects.create(
            user=self.request.user,
            action="CREATED_OFFER",
            ip_address=get_client_ip(self.request),
            details={"offer_id": instance.id, "title": instance.title, "target_role": instance.target_role}
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        AuditLog.objects.create(
            user=self.request.user,
            action="UPDATED_OFFER",
            ip_address=get_client_ip(self.request),
            details={"offer_id": instance.id, "title": instance.title}
        )

    def perform_destroy(self, instance):
        offer_id = instance.id
        title = instance.title
        instance.delete()
        AuditLog.objects.create(
            user=self.request.user,
            action="DELETED_OFFER",
            ip_address=get_client_ip(self.request),
            details={"offer_id": offer_id, "title": title}
        )

    @action(detail=True, methods=['post'], url_path='toggle-status')
    def toggle_status(self, request, pk=None):
        """Quick toggle to activate/pause an offer"""
        if request.user.role != 'SUPER_ADMIN':
            return Response(
                {"detail": "Only Super Admin can activate or deactivate offers."},
                status=status.HTTP_403_FORBIDDEN
            )
        offer = self.get_object()
        offer.is_active = not offer.is_active
        offer.save(update_fields=['is_active'])
        return Response({
            "message": f"Offer '{offer.title}' is now {'active' if offer.is_active else 'inactive'}.",
            "is_active": offer.is_active
        })

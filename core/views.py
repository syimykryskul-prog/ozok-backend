from django.utils.translation import gettext
from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from . import change_requests, settings_registry
from .exceptions import BusinessError
from .filters import build_filterset
from .models import AuditLog, ChangeRequest
from .permissions import FINANCE, SUPER_ADMIN, RolePermission
from .serializers import AuditLogSerializer, ChangeRequestSerializer


class SettingsListView(APIView):
    """GET settings/ — все читают; PATCH settings/<key>/ {value, reason} — super_admin, финансовые — и finance."""

    resource = "settings"
    permission_classes = [RolePermission]

    def get(self, request):
        data = settings_registry.all_settings()
        group = request.query_params.get("group")
        if group:
            data = [d for d in data if d["group"] == group]
        return Response(data)


class SettingDetailView(APIView):
    resource = "settings"
    permission_classes = [RolePermission]

    def _meta(self, key):
        meta = settings_registry.DEFAULTS.get(key)
        if meta is None:
            raise BusinessError(gettext("Неизвестная настройка."))
        return meta

    def get(self, request, key):
        self._meta(key)
        return Response(next(s for s in settings_registry.all_settings() if s["key"] == key))

    def patch(self, request, key):
        meta = self._meta(key)
        role = request.user.role
        if role != SUPER_ADMIN and not (role == FINANCE and meta["is_finance"]):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied(gettext("Эту настройку меняет только Super Admin OZOK."))
        if "value" not in request.data:
            raise BusinessError(gettext("Укажите value."))
        settings_registry.set_setting(key, request.data["value"], user=request.user,
                                      reason=request.data.get("reason"))
        return Response(next(s for s in settings_registry.all_settings() if s["key"] == key))

    put = patch


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    resource = "audit_log"
    permission_classes = [RolePermission]
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    ordering_fields = ("id", "created_at")
    search_fields = ("entity", "reason", "user_email")

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        for name in ("entity", "entity_id", "action"):
            if p.get(name):
                qs = qs.filter(**{name: p[name]})
        if p.get("user") and p["user"].isdigit():
            qs = qs.filter(user_id=int(p["user"]))
        for name, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
            d = parse_date(p[name]) if p.get(name) else None
            if d:
                qs = qs.filter(**{lookup: d})
        return qs


class ChangeRequestViewSet(viewsets.ReadOnlyModelViewSet):
    """change-requests/: партнёр видит свои запросы, OZOK — все; решают роли из настройки «Согласования»."""

    resource = "change_requests"
    permission_classes = [RolePermission]
    queryset = ChangeRequest.objects.select_related("requested_by", "decided_by")
    serializer_class = ChangeRequestSerializer
    ordering_fields = ("id", "created_at", "status")

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if getattr(self, "swagger_fake_view", False):
            return qs.none()
        if user.franchisee_id:
            qs = qs.filter(franchisee_id=user.franchisee_id)
        for name in ("status", "entity", "field"):
            if self.request.query_params.get(name):
                qs = qs.filter(**{name: self.request.query_params[name]})
        return qs

    def _decide(self, request, approve):
        cr = self.get_object()
        cr = change_requests.decide(cr, request.user, approve, request.data.get("comment", ""))
        return Response(self.get_serializer(cr).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._decide(request, True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, False)

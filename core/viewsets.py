from django.utils.translation import gettext, gettext_lazy
from django.db.models import ProtectedError, RestrictedError
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .exceptions import BusinessError
from .permissions import IMPLEMENTER, RECRUITER, REGIONAL_SUPPORT, RolePermission


def region_lookup(lookup):
    """franchisee_id -> franchisee__region; id -> region; client__franchisee_id -> client__franchisee__region."""
    if lookup == "id":
        return "region"
    base = lookup[:-3] if lookup.endswith("_id") else lookup
    return f"{base}__region"


class FranchiseeScopedMixin:
    """Изоляция данных.

    - сотрудник партнёра видит только объекты своего партнёра (franchisee_lookup);
    - рекрутёр — только recruiter_lookup = он (иначе ничего); региональная поддержка — своего региона;
      внедренец — implementer_lookup = он;
    - остальные сотрудники OZOK видят всё, с необязательным фильтром ?franchisee=<id>.
    """

    permission_classes = [RolePermission]
    resource = None
    franchisee_lookup = "franchisee_id"
    recruiter_lookup = None
    implementer_lookup = None
    region_field = None  # полный lookup региона, если он не выводится из franchisee_lookup
    partner_scope = None  # callable(partner_id) -> Q: видимость по закреплению филиала (C9)
    protected_message = gettext_lazy("Нельзя удалить: на объект есть ссылки в других данных.")

    def scope_queryset(self, qs):
        user = self.request.user
        if user.franchisee_id:
            if self.partner_scope is not None:
                return qs.filter(self.partner_scope(user.franchisee_id)).distinct()
            return qs.filter(**{self.franchisee_lookup: user.franchisee_id})
        from . import access
        scope = access.get_effective(self.request).scope
        if scope == "own_partners":
            return qs.filter(**{self.recruiter_lookup: user.id}) if self.recruiter_lookup else qs.none()
        if scope == "own_region":
            if not user.region:
                return qs.none()
            return qs.filter(**{(self.region_field or region_lookup(self.franchisee_lookup)) + "__iexact":
                                user.region})
        if scope == "assigned":
            return qs.filter(**{self.implementer_lookup: user.id}) if self.implementer_lookup else qs.none()
        fid = self.request.query_params.get("franchisee")
        if fid and fid.isdigit():
            qs = qs.filter(**{self.franchisee_lookup: int(fid)})
        return qs

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs.none()
        return self.scope_queryset(qs)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except (ProtectedError, RestrictedError):
            raise BusinessError(self.protected_message)


class ScopedViewSet(FranchiseeScopedMixin, viewsets.ModelViewSet):
    pass


class ScopedReadOnlyViewSet(FranchiseeScopedMixin, viewsets.ReadOnlyModelViewSet):
    pass


class CancelOnlyMixin:
    """Финансовый ресурс: DELETE запрещён, вместо него POST cancel/ {reason} (H7).

    queryset вьюсета строится от Model.all_objects. Список по умолчанию без отменённых;
    ?cancelled=true — только отменённые, ?cancelled=all — все. Отменённую запись менять нельзя."""

    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    cancel_service = None  # callable(obj, user, reason) -> obj; по умолчанию obj.mark_cancelled

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list":
            mode = self.request.query_params.get("cancelled")
            if mode == "true":
                qs = qs.filter(cancelled_at__isnull=False)
            elif mode != "all":
                qs = qs.filter(cancelled_at__isnull=True)
        return qs

    def perform_update(self, serializer):
        if serializer.instance.cancelled_at is not None:
            raise BusinessError(gettext("Отменённую запись изменять нельзя."))
        super().perform_update(serializer)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        obj = self.get_object()
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину отмены."))
        if self.cancel_service:
            obj = self.cancel_service(obj, request.user, reason)
        else:
            obj.mark_cancelled(request.user, reason)
        return Response(self.get_serializer(obj).data)

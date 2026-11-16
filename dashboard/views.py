from django.utils.translation import gettext
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import parse_month
from core.permissions import RolePermission

from .services import franchisee_dashboard, hq_dashboard


class DashboardView(APIView):
    """GET dashboard/?month=YYYY-MM — форма ответа зависит от пользователя (центр / франчайзи)."""

    resource = "dashboard"
    permission_classes = [RolePermission]

    def get(self, request):
        month = parse_month(request.query_params.get("month"))
        if request.user.franchisee_id:
            data = franchisee_dashboard(request.user.franchisee, month)
            if request.user.role in ("franchisee_staff", "manager"):  # менеджеру долги перед центром не показываем
                for key in ("balance", "overdue_amount", "commission_due"):
                    data[key] = None
            return Response(data)
        from .roles import BY_ROLE
        if request.user.role in BY_ROLE:
            return Response(BY_ROLE[request.user.role](request.user, month))
        return Response(hq_dashboard(month))


from rest_framework.exceptions import PermissionDenied  # noqa: E402

from core.exceptions import BusinessError  # noqa: E402
from network.models import Franchisee, Product  # noqa: E402

from . import analytics  # noqa: E402


def _resolve_partner(request, required=True):
    """Партнёр видит только себя (параметр partner игнорируется); OZOK указывает ?partner=."""
    user = request.user
    if user.franchisee_id:
        return user.franchisee
    pid = request.query_params.get("partner") or request.query_params.get("franchisee")
    if not (pid and str(pid).isdigit()):
        if required:
            raise BusinessError(gettext("Укажите partner."))
        return None
    partner = Franchisee.objects.filter(pk=int(pid)).first()
    if partner is None:
        raise BusinessError(gettext("Партнёр не найден."))
    return partner


class LedgerReportView(APIView):
    """GET reports/ledger/?partner=&date_from=&date_to= — доходы/расходы/прибыль (без partner — вся сеть для OZOK)."""

    resource = "ledger"
    permission_classes = [RolePermission]

    def get(self, request):
        d1, d2 = analytics.parse_range(request.query_params)
        partner = _resolve_partner(request, required=False)
        data = analytics.ledger_report([partner.id] if partner else None, d1, d2)
        data["partner"] = partner.id if partner else None
        return Response(data)


class PartnerAnalyticsView(APIView):
    """GET reports/partner-analytics/?partner=&date_from=&date_to= — всё по партнёру с разбивкой по сферам."""

    resource = "analytics"
    permission_classes = [RolePermission]

    def get(self, request):
        d1, d2 = analytics.parse_range(request.query_params)
        return Response(analytics.partner_analytics(_resolve_partner(request), d1, d2))


class OzokClientsAnalyticsView(APIView):
    """GET reports/ozok-clients/?product=&date_from=&date_to= — сегмент «Собственные клиенты OZOK» (только OZOK)."""

    resource = "analytics"
    permission_classes = [RolePermission]

    def get(self, request):
        if request.user.franchisee_id or request.user.role in ("recruiter", "implementer"):
            raise PermissionDenied(gettext("У вас нет прав для этого действия."))
        d1, d2 = analytics.parse_range(request.query_params)
        value = request.query_params.get("product")
        product = None
        if value:
            product = (Product.objects.filter(pk=int(value)) if value.isdigit()
                       else Product.objects.filter(code=value)).first()
            if product is None:
                raise BusinessError(gettext("Сфера не найдена."))
        return Response(analytics.ozok_clients_segment(d1, d2, product))


class SphereAnalyticsView(APIView):
    """GET reports/sphere-analytics/?product=&date_from=&date_to= — все партнёры сферы с местом (rank)."""

    resource = "analytics"
    permission_classes = [RolePermission]

    def get(self, request):
        d1, d2 = analytics.parse_range(request.query_params)
        value = request.query_params.get("product")
        if not value:
            raise BusinessError(gettext("Укажите product."))
        product = (Product.objects.filter(pk=int(value)) if value.isdigit() else Product.objects.filter(code=value)).first()
        if product is None:
            raise BusinessError(gettext("Сфера не найдена."))
        p = request.query_params
        user = request.user
        if user.role in ("recruiter", "implementer"):
            raise PermissionDenied(gettext("У вас нет прав для этого действия."))
        data = analytics.sphere_analytics(
            product, d1, d2, only_partner=user.franchisee_id, region=p.get("region"), status=p.get("status"),
            tier=p.get("tier"), lead_source=p.get("lead_source"))
        if user.franchisee_id:  # партнёр не видит данные собственных клиентов OZOK
            data.pop("ozok_clients", None)
            data.pop("ozok_share", None)
        return Response(data)

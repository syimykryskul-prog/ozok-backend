from django.utils.translation import gettext
from datetime import timedelta

from django.db.models import Exists, Max, OuterRef, Q
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import BusinessError
from core.filters import build_filterset, flag, product_filter
from core.models import today
from core.permissions import ADMIN, RolePermission
from core.viewsets import CancelOnlyMixin, ScopedReadOnlyViewSet, ScopedViewSet

from . import services
from . import branches as branch_svc
from . import deals
from . import subscriptions as sub_svc
from .deal_serializers import DealSerializer, preview_payload
from .models import (Branch, BranchAssignmentHistory, Client, ClientContact, ClientProductAccount, ClientSale, Deal,
                     Implementation, Subscription)
from .reports import sales_breakdown
from .serializers import (AssignmentHistorySerializer, BranchSerializer, ClientContactSerializer,
                          ClientProductAccountSerializer, ClientSaleSerializer, ClientSerializer,
                          ImplementationSerializer, StageSerializer, RENEWAL_DAYS)


LIVE = Subscription.LIVE_STATUSES


def _client_partner_scope(fid):
    """C9: клиент виден партнёру, если это его клиент или у клиента есть филиал, который партнёр обслуживает."""
    return (Q(franchisee_id=fid) | Q(branches__responsible_partner_id=fid) | Q(branches__implementer_partner_id=fid)
            | Q(branches__priority_partner_id=fid, branches__implementer_partner__isnull=True))


def _account_exists(client_ref, flt):
    return ClientProductAccount.objects.filter(client=OuterRef(client_ref), status__in=LIVE, **flt)


def _renewal_flt(on):
    return {"paid_until__gte": on, "paid_until__lte": on + timedelta(days=RENEWAL_DAYS)}


def _client_renewal(qs, name, value):
    cond = Exists(_account_exists("pk", _renewal_flt(today())))
    return qs.filter(cond) if value else qs.exclude(cond)


def _client_expired(qs, name, value):
    cond = Exists(_account_exists("pk", {"paid_until__lt": today()}))
    return qs.filter(cond) if value else qs.exclude(cond)


def _acc_renewal(qs, name, value):
    cond = Q(status__in=LIVE, **_renewal_flt(today()))
    return qs.filter(cond) if value else qs.exclude(cond)


def _acc_expired(qs, name, value):
    cond = Q(status__in=LIVE, paid_until__lt=today())
    return qs.filter(cond) if value else qs.exclude(cond)


class ClientViewSet(ScopedViewSet):
    resource = "clients"
    queryset = Client.objects.select_related("franchisee").prefetch_related(
        "accounts__product", "sales__product", "implementations__product", "branches", "contacts")
    serializer_class = ClientSerializer
    partner_scope = staticmethod(_client_partner_scope)
    filterset_class = build_filterset(Client, exact=("business_type", "city", "origin"), bools=("is_active",),
                                      renewal_soon=flag(_client_renewal), expired=flag(_client_expired))
    search_fields = ("business_name", "contact_name", "phone")
    ordering_fields = ("id", "business_name", "created_at", "city")

    def get_queryset(self):
        return super().get_queryset().order_by("-created_at", "-id").annotate(
            last_paid=Max("accounts__paid_until", filter=Q(accounts__status__in=LIVE)))

    def perform_destroy(self, instance):
        """Клиента с продажами или аккаунтами в продуктах удалить нельзя (каскад стёр бы историю денег)."""
        has_history = instance.sales.exists() or instance.accounts.exists()
        if has_history:
            if self.request.user.role != ADMIN:
                raise PermissionDenied(gettext("Клиента с продажами или аккаунтами в продуктах может удалять только "
                                       "администратор центра."))
            raise BusinessError(gettext("Нельзя удалить клиента с продажами или аккаунтами в продуктах. "
                                "Деактивируйте его: is_active=false."))
        super().perform_destroy(instance)


def _branch_scope(fid):
    return branch_svc.served_by(fid)


def _sub_scope(fid):
    return (Q(branch__responsible_partner_id=fid) | Q(branch__implementer_partner_id=fid)
            | Q(branch__priority_partner_id=fid, branch__implementer_partner__isnull=True))


class ClientProductAccountViewSet(ScopedReadOnlyViewSet):
    """client-accounts/ (совместимость): те же подписки, что и subscriptions/."""

    resource = "client_accounts"
    franchisee_lookup = "client__franchisee_id"
    partner_scope = staticmethod(_sub_scope)
    queryset = ClientProductAccount.objects.select_related("client__franchisee", "product", "branch", "tariff")
    serializer_class = ClientProductAccountSerializer
    filterset_class = build_filterset(ClientProductAccount, exact=("status",), ids=("client", "branch"),
                                      product=product_filter(), renewal_soon=flag(_acc_renewal),
                                      expired=flag(_acc_expired))
    search_fields = ("client__business_name", "external_id")
    ordering_fields = ("id", "paid_until", "connected_at")


class SubscriptionViewSet(ClientProductAccountViewSet):
    """subscriptions/: подписки филиалов; periods/ — помесячный разбор оплат."""

    resource = "subscriptions"

    @action(detail=True, methods=["get"])
    def periods(self, request, pk=None):
        sub = self.get_object()
        return Response({"subscription": sub.id, "branch": sub.branch_id, "periods": sub_svc.periods(sub)})

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        sub = self.get_object()
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину закрытия."))
        if sub.status == Subscription.Status.CLOSED:
            raise BusinessError(gettext("Подписка уже закрыта."))
        sub.status, sub.closed_at = Subscription.Status.CLOSED, today()
        sub.save(update_fields=["status", "closed_at", "updated_at"])
        from core import audit
        audit.log_event("status", sub, changes={"status": [None, "closed"]}, reason=reason, user=request.user)
        return Response(self.get_serializer(sub).data)


class BranchViewSet(ScopedViewSet):
    """branches/: филиалы клиентов. accept/ — приоритетный партнёр, reassign/ — OZOK, elqr-account/ — ручная привязка."""

    resource = "branches"
    franchisee_lookup = "responsible_partner_id"
    region_field = "region"
    partner_scope = staticmethod(_branch_scope)
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    queryset = Branch.objects.select_related("client", "product", "implementer_partner").prefetch_related(
        "assignment_history__from_partner", "assignment_history__to_partner", "subscriptions__product")
    serializer_class = BranchSerializer
    filterset_class = build_filterset(Branch, exact=("status",), ids=("client", "implementer_partner",
                                                                      "responsible_partner", "priority_partner"),
                                      bools=("is_main",), product=product_filter())
    search_fields = ("name", "client__business_name", "elqr_account")
    ordering_fields = ("id", "name", "created_at")

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        branch = branch_svc.accept(self.get_object(), request.user)
        return Response(self.get_serializer(branch).data)

    @action(detail=True, methods=["post"])
    def reassign(self, request, pk=None):
        from network.models import Franchisee
        d = request.data
        partner = Franchisee.objects.filter(pk=d.get("partner")).first() if str(d.get("partner") or "").isdigit() \
            else None
        if partner is None:
            raise BusinessError(gettext("Укажите партнёра (partner)."))
        eff = None
        if d.get("effective_from"):
            from django.utils.dateparse import parse_date
            eff = parse_date(str(d["effective_from"]))
            if eff is None:
                raise BusinessError(gettext("Неверная дата effective_from."))
        branch = branch_svc.reassign(self.get_object(), partner, reason_code=str(d.get("reason_code") or ""),
                                     reason=str(d.get("reason") or ""), user=request.user, effective_from=eff)
        return Response(self.get_serializer(branch).data)

    @action(detail=True, methods=["post"], url_path="commission-switch")
    def commission_switch(self, request, pk=None):
        """Только super_admin: включить/выключить комиссию сопровождающему партнёру для филиала клиента OZOK
        {enabled, effective_from?, reason} — с аудитом и датой начала."""
        d = request.data
        eff = None
        if d.get("effective_from"):
            from django.utils.dateparse import parse_date
            eff = parse_date(str(d["effective_from"]))
            if eff is None:
                raise BusinessError(gettext("Неверная дата effective_from."))
        enabled = d.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("1", "true", "yes")
        branch = branch_svc.set_commission_switch(self.get_object(), bool(enabled), effective_from=eff,
                                                  reason=str(d.get("reason") or ""), user=request.user)
        return Response(self.get_serializer(branch).data)

    @action(detail=True, methods=["post"], url_path="elqr-account")
    def elqr_account(self, request, pk=None):
        branch = self.get_object()
        ser = BranchSerializer(branch, data={"elqr_account": request.data.get("elqr_account")}, partial=True,
                               context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        if not ser.validated_data.get("elqr_account"):
            raise BusinessError(gettext("Укажите номер лицевого счёта Элкюар."))
        branch.elqr_account = ser.validated_data["elqr_account"]
        branch.save(update_fields=["elqr_account", "updated_at"])
        return Response(self.get_serializer(branch).data)


class BranchAssignmentViewSet(ScopedReadOnlyViewSet):
    """branch-assignments/: «Передачи клиентов» — история закрепления филиалов (фильтр reason_code=partner_closed)."""

    resource = "branch_assignments"
    franchisee_lookup = "branch__responsible_partner_id"
    partner_scope = staticmethod(lambda fid: Q(from_partner_id=fid) | Q(to_partner_id=fid))
    queryset = BranchAssignmentHistory.objects.select_related("branch__client", "from_partner", "to_partner",
                                                             "decided_by")
    serializer_class = AssignmentHistorySerializer
    filterset_class = build_filterset(BranchAssignmentHistory, exact=("reason_code", "field"),
                                      ids=("branch", "from_partner", "to_partner"), date_field="effective_from")
    ordering_fields = ("id", "effective_from")


class ClientContactViewSet(ScopedViewSet):
    resource = "client_contacts"
    franchisee_lookup = "client__franchisee_id"
    partner_scope = staticmethod(lambda fid: Q(client__franchisee_id=fid)
                                 | Q(client__branches__responsible_partner_id=fid)
                                 | Q(client__branches__implementer_partner_id=fid))
    queryset = ClientContact.objects.select_related("client", "branch")
    serializer_class = ClientContactSerializer
    filterset_class = build_filterset(ClientContact, ids=("client", "branch"), bools=("is_primary",))
    search_fields = ("full_name", "phone", "email")


def _impl_overdue(qs, name, value):
    cond = Q(planned_launch__lt=today()) & ~Q(stage__in=("launched", "cancelled"))
    return qs.filter(cond) if value else qs.exclude(cond)


class ImplementationViewSet(ScopedViewSet):
    resource = "implementations"
    implementer_lookup = "responsible_id"
    queryset = Implementation.objects.select_related("franchisee", "client", "product", "responsible")
    serializer_class = ImplementationSerializer
    filterset_class = build_filterset(Implementation, exact=("stage",), ids=("client",), product=product_filter(),
                                      overdue=flag(_impl_overdue))
    search_fields = ("client__business_name",)
    ordering_fields = ("id", "started_at", "planned_launch", "stage")

    @action(detail=True, methods=["post"])
    def stage(self, request, pk=None):
        ser = StageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        impl = services.set_stage(self.get_object(), ser.validated_data["stage"])
        return Response(self.get_serializer(impl).data)


class ClientSaleViewSet(CancelOnlyMixin, ScopedViewSet):
    resource = "client_sales"
    queryset = ClientSale.all_objects.select_related("franchisee", "client", "product")
    serializer_class = ClientSaleSerializer
    filterset_class = build_filterset(ClientSale, exact=("kind",), ids=("client", "tariff", "deal"), date_field="date",
                                      product=product_filter())
    search_fields = ("client__business_name",)
    ordering_fields = ("id", "date", "amount", "commission_amount")

    @action(detail=True, methods=["post"])
    def recalculate(self, request, pk=None):
        """Пересчитать продажу по текущим правилам вознаграждения (super_admin, finance)."""
        sale = services.recalculate_sale(self.get_object())
        return Response(self.get_serializer(sale).data)

    def cancel_service(self, sale, user, reason):
        """Отмена вместо удаления. Франчайзи отменяет только свои ручные продажи без вознаграждения."""
        if user.franchisee_id:
            own_manual = (sale.source == ClientSale.Source.MANUAL and sale.kind not in ClientSale.COMMISSION_KINDS
                          and sale.created_by_id is not None
                          and sale.created_by.franchisee_id == user.franchisee_id)
            if not own_manual:
                raise PermissionDenied(gettext("Эту продажу может отменить только OZOK."))
        return services.cancel_sale(sale, user, reason)


class DealViewSet(ScopedViewSet):
    """deals/: продажа одной формой. Партнёр — свои; OZOK видит все, включая прямые продажи (partner=null)."""

    resource = "deals"
    franchisee_lookup = "partner_id"
    queryset = Deal.objects.select_related("client", "partner", "product").prefetch_related(
        "lines__equipment", "lines__tariff")
    serializer_class = DealSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    filterset_class = build_filterset(Deal, exact=("status",), ids=("client", "partner"), date_field="date",
                                      product=product_filter(),
                                      nbs_direct=flag(lambda qs, n, v: qs.filter(partner__isnull=bool(v))))
    search_fields = ("client__business_name", "client_name")
    ordering_fields = ("id", "date", "total", "status")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role in ("regional_support",):
            return qs.none()
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        if user.franchisee_id is None and user.role not in ("super_admin", "network_head"):
            raise PermissionDenied(gettext("Продажи вносят партнёры и OZOK (Super Admin, руководитель сети)."))
        super().perform_create(serializer)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Расчёт до сохранения: доход партнёра, доля OZOK, удержание по строкам."""
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        attrs = dict(ser.validated_data)
        client, partner = attrs["_client"], attrs["_partner"]
        lead = attrs.get("lead") or (client.lead if client else None)
        computed = deals.compute_deal(
            product=attrs["product"], partner=partner, date=attrs.get("date"), lead=lead,
            lines=[dict(l) for l in attrs.get("lines", [])], deal_discount_percent=attrs.get("deal_discount_percent", 0),
            deal_discount_amount=attrs.get("deal_discount_amount", 0))
        return Response(preview_payload(computed, product=attrs["product"], partner=partner))

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        deal = deals.confirm_deal(self.get_object(), request.user)
        return Response(self.get_serializer(deal).data)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        """Финансист/Super Admin отмечает подтверждённую сделку оплаченной (AC1: двигает воронку менеджера)."""
        return Response(self.get_serializer(deals.mark_paid(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        deal = deals.cancel_deal(self.get_object(), request.user, request.data.get("reason"))
        return Response(self.get_serializer(deal).data)


class SalesReportView(APIView):
    """GET reports/sales/?date_from=&date_to=&franchisee=&product= (раздел 5А)."""

    resource = "sales_report"
    permission_classes = [RolePermission]

    def get(self, request):
        from datetime import date as _date

        from django.utils.dateparse import parse_date

        from core.models import month_first, month_last
        p = request.query_params
        first, last = month_first(today()), month_last(today())
        try:
            date_from = parse_date(p["date_from"]) if p.get("date_from") else first
            date_to = parse_date(p["date_to"]) if p.get("date_to") else last
        except ValueError:
            date_from = date_to = None
        if not isinstance(date_from, _date) or not isinstance(date_to, _date) or date_from > date_to:
            raise BusinessError(gettext("Неверный период: укажите date_from и date_to в формате YYYY-MM-DD."))
        user = request.user
        if user.franchisee_id:
            fid = user.franchisee_id
        else:
            fid = int(p["franchisee"]) if p.get("franchisee", "").isdigit() else None
        code = p.get("product") or None
        data = sales_breakdown(date_from, date_to, fid, code, with_franchisees=not user.franchisee_id)
        return Response({"period": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
                         "filters": {"franchisee": fid if not user.franchisee_id else None, "product": code},
                         **data})




# ---- P7 ---------------------------------------------------------------------------------------------------------
from rest_framework import mixins, viewsets  # noqa: E402

from . import installations as inst_svc  # noqa: E402
from .models import ChecklistTemplate, ContractTemplate, ImplementerAccrual, InstallationComment  # noqa: E402
from .serializers import (ChecklistTemplateSerializer, ContractTemplateSerializer,  # noqa: E402
                          EquipmentSaleSerializer, ImplementerAccrualSerializer, InstallationCommentSerializer,
                          InstallationSerializer)


class InstallationViewSet(ScopedViewSet):
    """installations/: установки (расширенные внедрения). Внедренец видит свои; процесс §9.1 — actions."""

    resource = "installations"
    implementer_lookup = "implementer_id"
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    queryset = Implementation.objects.select_related("franchisee", "client", "product", "implementer",
                                                    "responsible", "branch")
    serializer_class = InstallationSerializer
    filterset_class = build_filterset(Implementation, exact=("status", "stage", "training_result"),
                                      ids=("client", "branch", "implementer", "deal"), product=product_filter(),
                                      overdue=flag(_impl_overdue))
    search_fields = ("client__business_name",)
    ordering_fields = ("id", "started_at", "deadline", "status")

    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        inst = inst_svc.set_status(self.get_object(), str(request.data.get("status") or ""), request.user)
        return Response(self.get_serializer(inst).data)

    @action(detail=True, methods=["post"])
    def checklist(self, request, pk=None):
        items = request.data.get("items")
        if not isinstance(items, list):
            raise BusinessError(gettext("Передайте items: список пунктов."))
        return Response(self.get_serializer(inst_svc.update_checklist(self.get_object(), items)).data)

    @action(detail=True, methods=["post"])
    def channels(self, request, pk=None):
        inst = inst_svc.set_channel(self.get_object(), str(request.data.get("channel") or ""),
                                    str(request.data.get("status") or ""))
        return Response(self.get_serializer(inst).data)

    @action(detail=True, methods=["post"], url_path="training-result")
    def training_result(self, request, pk=None):
        inst = inst_svc.set_training_result(self.get_object(), str(request.data.get("result") or ""))
        return Response(self.get_serializer(inst).data)

    @action(detail=True, methods=["post"])
    def act(self, request, pk=None):
        inst = inst_svc.confirm_act(self.get_object(), confirmed_by=str(request.data.get("client_confirmed_by") or ""),
                                    act_file=str(request.data.get("act_file") or ""), user=request.user)
        return Response(self.get_serializer(inst).data)

    @action(detail=True, methods=["post"])
    def rate(self, request, pk=None):
        rating = request.data.get("rating")
        inst = inst_svc.rate(self.get_object(), rating if isinstance(rating, int) else None)
        return Response(self.get_serializer(inst).data)

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        inst = self.get_object()
        if request.method == "POST":
            c = inst_svc.add_comment(inst, request.user, str(request.data.get("text") or ""))
            return Response(InstallationCommentSerializer(c).data, status=201)
        return Response(InstallationCommentSerializer(inst.comments.select_related("author"), many=True).data)


class ImplementerAccrualViewSet(ScopedReadOnlyViewSet):
    resource = "implementer_accruals"
    franchisee_lookup = "installation__franchisee_id"
    implementer_lookup = "user_id"
    queryset = ImplementerAccrual.objects.select_related("installation__client", "installation__product", "user")
    serializer_class = ImplementerAccrualSerializer
    filterset_class = build_filterset(ImplementerAccrual, exact=("status",), ids=("user", "installation"))
    ordering_fields = ("id", "amount", "status")

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        return Response(self.get_serializer(inst_svc.verify_accrual(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        acc = inst_svc.pay_accrual(self.get_object(), request.user, str(request.data.get("payment_ref") or ""))
        return Response(self.get_serializer(acc).data)


class ChecklistTemplateViewSet(viewsets.ModelViewSet):
    resource = "checklist_templates"
    permission_classes = [RolePermission]
    queryset = ChecklistTemplate.objects.select_related("product")
    serializer_class = ChecklistTemplateSerializer
    filterset_class = build_filterset(ChecklistTemplate, ids=("product",), bools=("is_active",))
    http_method_names = ["get", "post", "put", "patch", "head", "options"]


class ContractTemplateViewSet(viewsets.ModelViewSet):
    resource = "contract_templates"
    permission_classes = [RolePermission]
    queryset = ContractTemplate.objects.all()
    serializer_class = ContractTemplateSerializer
    filterset_class = build_filterset(ContractTemplate, exact=("kind",), bools=("is_active",))
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.franchisee_id:  # партнёр видит активные шаблоны для партнёров и клиентов, не чужих менеджеров
            qs = qs.filter(is_active=True, kind__in=("franchisee", "client", "manager"))
        return qs


class EquipmentSaleViewSet(CancelOnlyMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                           mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """equipment-sales/: продажи оборудования с серийниками, доставкой и гарантией. Создаются продажей (deals/)."""

    resource = "equipment_sales"
    permission_classes = [RolePermission]
    queryset = ClientSale.all_objects.filter(kind="equipment").select_related("client", "equipment", "sold_by",
                                                                              "installed_by")
    serializer_class = EquipmentSaleSerializer
    filterset_class = build_filterset(ClientSale, exact=("delivery_status",), ids=("client", "equipment"),
                                      date_field="date")
    http_method_names = ["get", "patch", "head", "options"]
    search_fields = ("client__business_name",)
    ordering_fields = ("id", "date")

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.franchisee_id:
            qs = qs.filter(franchisee_id=user.franchisee_id)
        elif user.role == "regional_support":
            qs = qs.filter(franchisee__region__iexact=user.region) if user.region else qs.none()
        return qs

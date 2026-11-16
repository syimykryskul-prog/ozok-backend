from django.utils.translation import gettext
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from billing.models import Charge, FranchiseePayment
from billing.services import franchisee_finance
from clients.models import ClientSale
from clients.reports import sales_by_product
from core.exceptions import BusinessError
from django_filters import rest_framework as df

from core.filters import build_filterset, product_filter
from core.models import money, month_first, month_last, today
from core.permissions import LIMITED_PARTNER_ROLES, RolePermission
from core.viewsets import ScopedViewSet

from . import services
from .commission import preview as rule_preview, set_price
from .models import (CommissionRule, Contract, Equipment, FranchiseOffer, Franchisee, Package, PartnerStatusHistory,
                     Product, Tariff)
from . import partners as partner_svc
from .serializers import (CommissionRuleSerializer, ContractSerializer, EquipmentSerializer,
                          FranchiseOfferSerializer, FranchiseeSerializer, PartnerStatusHistorySerializer,
                          PackageSerializer, ProductSerializer, SetPriceSerializer, TariffSerializer)


def product_in_use(product):
    from clients.models import ClientProductAccount, Implementation
    from training.models import Course
    return (product.contracts.exists() or product.direct_contracts.exists() or product.sales.exists()
            or product.accounts.exists() or Implementation.objects.filter(product=product).exists()
            or Course.objects.filter(product=product).exists() or product.packages.exists())


class ProductViewSet(viewsets.ModelViewSet):
    resource = "products"
    permission_classes = [RolePermission]
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    filterset_class = build_filterset(Product, bools=("is_active",))
    search_fields = ("name", "code", "sphere")
    ordering_fields = ("id", "name", "sort_order", "price_cash")

    def perform_destroy(self, instance):
        if product_in_use(instance):
            raise BusinessError(gettext("Нельзя удалить сферу, которая используется в договорах, продажах или клиентах. "
                                "Отключите её (is_active=false)."))
        instance.delete()

    @action(detail=True, methods=["post"], url_path="rotate-key")
    def rotate_key(self, request, pk=None):
        product = self.get_object()
        product.api_key = Product.generate_key()
        product.save(update_fields=["api_key", "updated_at"])
        return Response(self.get_serializer(product).data)

    @action(detail=True, methods=["post"], url_path="test-event")
    def test_event(self, request, pk=None):
        """Тестовое событие: симулирует оплату демо-клиента (для проверки без реального продукта)."""
        from integrations.simulator import simulate
        return Response({"results": simulate(self.get_object(), 1)})

    @action(detail=True, methods=["post"], url_path="request")
    def connect_request(self, request, pk=None):
        lead = services.request_connection(request.user, product=self.get_object())
        return Response({"lead_id": lead.id}, status=201)


class PackageViewSet(viewsets.ModelViewSet):
    resource = "packages"
    permission_classes = [RolePermission]
    queryset = Package.objects.prefetch_related("products")
    serializer_class = PackageSerializer
    filterset_class = build_filterset(Package, bools=("is_active",))
    search_fields = ("name",)
    ordering_fields = ("id", "name", "price_cash")

    @action(detail=True, methods=["post"], url_path="request")
    def connect_request(self, request, pk=None):
        lead = services.request_connection(request.user, package=self.get_object())
        return Response({"lead_id": lead.id}, status=201)


class FranchiseeViewSet(ScopedViewSet):
    resource = "franchisees"
    franchisee_lookup = "id"
    recruiter_lookup = "recruited_by_id"
    queryset = Franchisee.objects.all()
    serializer_class = FranchiseeSerializer
    search_fields = ("name", "city", "owner_name", "phone")
    ordering_fields = ("id", "name", "city", "status", "joined_at", "balance")

    def get_queryset(self):
        partner_svc.apply_due_changes()
        charges = (Charge.objects.filter(franchisee=OuterRef("pk")).values("franchisee")
                   .annotate(s=Sum("amount")).values("s"))
        paid = (FranchiseePayment.objects.filter(franchisee=OuterRef("pk")).values("franchisee")
                .annotate(s=Sum("amount")).values("s"))
        zero = Value(Decimal("0"), output_field=DecimalField(max_digits=14, decimal_places=2))
        qs = (super().get_queryset().select_related("curator", "recruited_by", "responsible").order_by("id")
              .annotate(balance=Coalesce(Subquery(charges), zero) - Coalesce(Subquery(paid), zero))
              .annotate(employees_n=Count("users", filter=Q(users__is_active=True), distinct=True),
                        clients_n=Count("clients", filter=Q(clients__is_active=True), distinct=True))
              .prefetch_related(Prefetch("contracts", queryset=Contract.objects.filter(status="active")
                                         .prefetch_related("products"), to_attr="active_contracts")))
        p = self.request.query_params
        if p.get("status"):
            qs = qs.filter(status__in=p["status"].split(","))
        for name in ("funnel_stage", "tier", "region", "candidate_source"):
            if p.get(name):
                qs = qs.filter(**{f"{name}__in": p[name].split(",")})
        for name in ("recruited_by", "responsible"):
            if p.get(name, "").isdigit():
                qs = qs.filter(**{f"{name}_id": int(p[name])})
        if p.get("stuck_days", "").isdigit():  # «застряли» на этапе дольше N дней
            qs = qs.filter(funnel_stage_since__lte=today() - __import__("datetime").timedelta(days=int(p["stuck_days"])),
                           status__in=("candidate", "trainee", "manager"))
        if p.get("city"):
            qs = qs.filter(city=p["city"])
        curator = p.get("curator")
        if curator == "me":
            qs = qs.filter(curator_id=self.request.user.id)
        elif curator and curator.isdigit():
            qs = qs.filter(curator_id=int(curator))
        return qs

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        from accounts.models import User
        from leads.models import Lead
        from leads.serializers import LeadSerializer
        from clients.serializers import ClientSaleSerializer
        from planning.services import plan_summary, rating_map
        from quality.models import Audit
        from quality.serializers import AuditSerializer
        from tasks.models import Task
        from tasks.serializers import TaskSerializer

        fr = self.get_object()
        ctx = self.get_serializer_context()
        fin = franchisee_finance(fr.id)
        month = month_first(today())
        r = rating_map(month).get(fr.id)
        contracts = fr.contracts.select_related("franchisee", "product", "package").prefetch_related("products")
        employees = User.objects.filter(franchisee=fr).order_by("id")
        last_audit = Audit.objects.filter(franchisee=fr).select_related("franchisee", "inspector").first()
        data = {
            "franchisee": FranchiseeSerializer(fr, context=ctx).data,
            "balance": money(fin["balance"]), "overdue_amount": money(fin["overdue"]),
            "commission_due": money(fin["commission_due"]),
            "commission_month": money(sum((s.commission_amount for s in ClientSale.objects.filter(
                franchisee=fr, date__gte=month, date__lte=month_last(month))), 0)),
            "sales_by_product": sales_by_product(month, month_last(month), fr.id),
            "rating": r["rating"] if r else None, "place": r["place"] if r else None,
            "plan": plan_summary(fr.id, month),
            "contracts": ContractSerializer(contracts, many=True, context=ctx).data,
            "employees": [{"id": u.id, "full_name": u.full_name, "email": u.email, "phone": u.phone,
                           "role": u.role, "is_active": u.is_active} for u in employees],
            "recent_leads": LeadSerializer(Lead.objects.filter(franchisee=fr).select_related("franchisee")[:10],
                                           many=True, context=ctx).data,
            "recent_sales": ClientSaleSerializer(
                ClientSale.objects.filter(franchisee=fr).select_related("client", "product", "franchisee")[:10],
                many=True, context=ctx).data,
            "open_tasks": TaskSerializer(
                Task.objects.filter(franchisee=fr, status__in=Task.ACTIVE_STATUSES)
                .select_related("franchisee", "created_by", "assignee")[:10], many=True, context=ctx).data,
            "last_audit": AuditSerializer(last_audit, context=ctx).data if last_audit else None,
        }
        if request.user.role in LIMITED_PARTNER_ROLES:  # сотруднику/менеджеру договоры и долги не показываем
            for key in ("balance", "overdue_amount", "commission_due", "contracts"):
                data[key] = None
        if request.user.role in ("recruiter", "regional_support", "quality", "implementer"):  # M-8: без финансов
            for key in ("balance", "overdue_amount", "commission_due", "commission_month", "sales_by_product",
                        "contracts"):
                data[key] = None
            data["recent_sales"] = []
        return Response(data)


    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        """franchisees/{id}/reopen/ {reason, status?} — только Super Admin; закрытый партнёр возвращается с аудитом."""
        from core import audit
        partner = self.get_object()
        if partner.status != Franchisee.Status.CLOSED:
            raise BusinessError(gettext("Партнёр не закрыт."))
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину возврата партнёра."))
        status = request.data.get("status") or "candidate"
        if status == Franchisee.Status.CLOSED or status not in Franchisee.Status.values:
            raise BusinessError(gettext("Недопустимый статус возврата."))
        partner_svc.change_partner_field(partner, "status", status, user=request.user, reason=reason)
        partner.refresh_from_db()
        partner.closed_at = None
        partner.save(update_fields=["closed_at", "updated_at"])
        audit.log_event("status", partner, changes={"status": ["closed", status]}, reason=gettext('Возврат: {reason}').format(reason=reason),
                        user=request.user)
        return Response(self.get_serializer(partner).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """franchisees/{id}/close/ {closed_at, reason, transfer_to_partner | transfer_to_user} (P5, H3)."""
        from accounts.models import User
        from django.utils.dateparse import parse_date
        from . import closure
        d = request.data
        closed_at = parse_date(str(d["closed_at"])) if d.get("closed_at") else None
        if d.get("closed_at") and closed_at is None:
            raise BusinessError(gettext("Неверная дата closed_at."))
        to_partner = Franchisee.objects.filter(pk=d["transfer_to_partner"]).first()             if str(d.get("transfer_to_partner") or "").isdigit() else None
        to_user = User.objects.filter(pk=d["transfer_to_user"]).first()             if str(d.get("transfer_to_user") or "").isdigit() else None
        if d.get("transfer_to_partner") and to_partner is None:
            raise BusinessError(gettext("Принимающий партнёр не найден."))
        if d.get("transfer_to_user") and to_user is None:
            raise BusinessError(gettext("Принимающий сотрудник не найден."))
        res = closure.close_partner(self.get_object(), user=request.user, closed_at=closed_at,
                                    reason=d.get("reason"), transfer_to_partner=to_partner, transfer_to_user=to_user)
        data = self.get_serializer(res.pop("partner")).data
        return Response({**data, "transfer": res})

    @action(detail=False, methods=["get"])
    def funnel(self, request):
        """Канбан кандидатов: этапы воронки -> партнёры (кто отвечает, дни на этапе, кто привлёк)."""
        from core.settings_registry import get_int
        stuck = get_int("funnel.stuck_days")
        qs = self.filter_queryset(self.get_queryset()).exclude(status__in=("franchisee", "suspended", "closed"))
        columns = {k: [] for k in Franchisee.STAGE_ORDER}
        for f in qs:
            days = partner_svc.days_in_stage(f)
            columns.setdefault(f.funnel_stage, []).append({
                "id": f.id, "name": f.name, "full_name": f.full_name, "status": f.status, "city": f.city,
                "responsible": f.responsible_id, "responsible_name": f.responsible.full_name if f.responsible else None,
                "recruited_by": f.recruited_by_id, "recruited_by_name": f.recruited_by.full_name if f.recruited_by else None,
                "days_in_stage": days, "stuck": days > stuck})
        return Response({"stuck_days": stuck, "stages": [
            {"stage": k, "label": dict(Franchisee.Stage.choices)[k], "count": len(v), "items": v}
            for k, v in columns.items() if k in dict(Franchisee.Stage.choices)]})


class PartnerStatusHistoryViewSet(viewsets.ModelViewSet):
    """partner-status-history/: журнал статуса/уровня; POST — смена с датой effective_from (OZOK)."""

    resource = "partner_history"
    permission_classes = [RolePermission]
    queryset = PartnerStatusHistory.objects.select_related("partner", "decided_by")
    serializer_class = PartnerStatusHistorySerializer
    filterset_class = build_filterset(PartnerStatusHistory, exact=("field",), ids=("partner",))
    http_method_names = ["get", "post", "head", "options"]
    ordering_fields = ("id", "effective_from")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.franchisee_id:
            qs = qs.filter(partner_id=self.request.user.franchisee_id)
        return qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        hist = partner_svc.change_partner_field(
            d["partner"], d["field"], d["new"], user=request.user, reason=d.get("reason", ""),
            effective_from=d.get("effective_from") if "effective_from" in request.data else None)
        if hist is None:
            raise BusinessError(gettext("Значение уже установлено."))
        return Response(self.get_serializer(hist).data, status=201)


class FranchiseOfferViewSet(viewsets.ModelViewSet):
    resource = "franchise_offers"
    permission_classes = [RolePermission]
    queryset = FranchiseOffer.objects.all()
    serializer_class = FranchiseOfferSerializer
    filterset_class = build_filterset(FranchiseOffer, bools=("is_active",))
    http_method_names = ["get", "post", "put", "patch", "head", "options"]  # предложения отключаются


class ContractViewSet(ScopedViewSet):
    resource = "contracts"
    queryset = Contract.objects.select_related("franchisee", "product", "package").prefetch_related("products")
    serializer_class = ContractSerializer
    filterset_class = build_filterset(Contract, exact=("status", "payment_scheme"))
    search_fields = ("number",)
    ordering_fields = ("id", "number", "signed_at", "created_at", "fee_total", "status")

    def perform_destroy(self, instance):
        if instance.status != Contract.Status.DRAFT:
            raise BusinessError(gettext("Удалить можно только черновик договора."))
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"], url_path="price-quote")
    def price_quote(self, request):
        p = request.query_params
        product = package = None
        if p.get("product"):
            v = p["product"]
            product = (Product.objects.filter(pk=int(v)) if v.isdigit() else Product.objects.filter(code=v)).first()
            if product is None:
                raise BusinessError(gettext("Сфера не найдена."))
        if p.get("package"):
            package = Package.objects.filter(pk=p["package"]).first() if p["package"].isdigit() else None
            if package is None:
                raise BusinessError(gettext("Пакет не найден."))
        quote = services.price_quote(product=product, package=package, scheme=p.get("scheme", "cash"))
        return Response({k: (money(v) if k in ("list_price", "prepay_percent", "commission_percent") else v)
                         for k, v in quote.items()})

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """L-6: активировать договор можно партнёру, дошедшему минимум до этапа «менеджер»; исключение — Super Admin
        с причиной (override_reason)."""
        c = self.get_object()
        order = Franchisee.STAGE_ORDER
        if order.index(c.franchisee.funnel_stage) < order.index("manager"):
            reason = str(request.data.get("override_reason") or "").strip()
            if not (request.user.role == "super_admin" and reason):
                raise BusinessError(gettext("Партнёр ещё не дошёл до этапа «менеджер»: договор активирует только "
                                    "Super Admin с причиной (override_reason)."))
            from core import audit
            audit.log_event("update", c, reason=gettext('Активация до этапа «менеджер»: {reason}').format(reason=reason), user=request.user)
        contract = services.activate_contract(c)
        return Response(self.get_serializer(contract).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        contract = services.close_contract(self.get_object())
        return Response(self.get_serializer(contract).data)




class TariffViewSet(viewsets.ModelViewSet):
    resource = "tariffs"
    permission_classes = [RolePermission]
    queryset = Tariff.objects.select_related("product").prefetch_related("prices")
    serializer_class = TariffSerializer
    filterset_class = build_filterset(Tariff, bools=("is_active",), product=product_filter())
    search_fields = ("name", "code")
    ordering_fields = ("id", "name", "sort_order")

    def perform_destroy(self, instance):
        if instance.sales.exists() or instance.accounts.exists():
            raise BusinessError(gettext("Тариф используется в продажах или у клиентов. Отключите его (is_active=false)."))
        instance.delete()

    def perform_update(self, serializer):
        from core import audit
        inst = serializer.instance
        new_min = serializer.validated_data.get("min_price", inst.min_price)
        if new_min != inst.min_price:  # L-5: минимальная цена — только с причиной (в аудит)
            reason = str(self.request.data.get("reason") or "").strip()
            if not reason:
                raise BusinessError(gettext("Для изменения минимальной цены укажите причину (reason)."))
            audit.log_event("update", inst, reason=reason, user=self.request.user,
                            changes={"min_price": [str(inst.min_price), str(new_min)]})
        serializer.save()

    @action(detail=True, methods=["post"], url_path="set-price")
    def set_price(self, request, pk=None):
        ser = SetPriceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tariff = self.get_object()
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину изменения цены."))
        old = tariff.prices.filter(valid_to__isnull=True).order_by("-valid_from", "-id").first()
        set_price(tariff, ser.validated_data["monthly_price"], ser.validated_data["valid_from"])
        from core import audit
        audit.log_event("update", tariff, reason=reason, user=request.user, changes={"monthly_price": [
            str(old.monthly_price) if old else None, str(ser.validated_data["monthly_price"])],
            "valid_from": [None, str(ser.validated_data["valid_from"])]})
        tariff = self.get_queryset().get(pk=tariff.pk)
        return Response(self.get_serializer(tariff).data)


class EquipmentViewSet(viewsets.ModelViewSet):
    resource = "equipment"
    permission_classes = [RolePermission]
    queryset = Equipment.objects.select_related("product")
    serializer_class = EquipmentSerializer
    filterset_class = build_filterset(Equipment, bools=("is_active",), product=product_filter())
    search_fields = ("name",)
    ordering_fields = ("id", "name", "price", "sort_order")

    def perform_destroy(self, instance):
        if instance.sales.exists():
            raise BusinessError(gettext("Оборудование есть в продажах. Отключите его (is_active=false)."))
        instance.delete()


def _active_on(qs, name, value):
    from django.utils.dateparse import parse_date
    on = parse_date(value) if value else None
    if on is None:
        return qs
    return qs.filter(is_active=True, valid_from__lte=on).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on))


class CommissionRuleViewSet(viewsets.ModelViewSet):
    resource = "commission_rules"
    permission_classes = [RolePermission]
    queryset = CommissionRule.objects.select_related("franchisee", "product", "tariff")
    serializer_class = CommissionRuleSerializer
    filterset_class = build_filterset(CommissionRule, exact=("sale_kind",), ids=("franchisee", "tariff"),
                                      product=product_filter(), active_on=df.CharFilter(method=_active_on))
    search_fields = ("name", "note")
    ordering_fields = ("id", "name", "valid_from")

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.franchisee_id:  # франчайзи читает только применимые к нему правила
            qs = qs.filter(Q(franchisee__isnull=True) | Q(franchisee_id=user.franchisee_id))
        return qs

    def perform_update(self, serializer):
        from core import audit
        reason = str(self.request.data.get("reason") or "").strip()
        rule = serializer.save()
        audit.log_event("update", rule, reason=reason, user=self.request.user,
                        changes={"fields": [None, sorted(serializer.validated_data)]})

    @action(detail=True, methods=["post"], url_path="new-version")
    def new_version(self, request, pk=None):
        """Новая версия правила с датой начала (не в прошлом); старая закрывается днём раньше. Причина обязательна."""
        from datetime import timedelta

        from django.utils.dateparse import parse_date

        from core import audit
        old = self.get_object()
        d = request.data
        reason = str(d.get("reason") or "").strip()
        if not reason:
            raise BusinessError(gettext("Укажите причину изменения правила."))
        start = parse_date(str(d["valid_from"])) if d.get("valid_from") else today() + timedelta(days=1)
        if start is None or start < today():
            raise BusinessError(gettext("Дата начала новой версии не может быть в прошлом."))
        if start <= old.valid_from:
            raise BusinessError(gettext("Дата начала новой версии должна быть позже начала действующей."))
        fields = {f: getattr(old, f) for f in ("name", "product", "tariff", "franchisee", "sale_kind", "partner_tier",
                                               "lead_source", "basis", "percent", "fixed_amount", "fixed_per",
                                               "base", "note", "is_active")}
        ser = CommissionRuleSerializer(data={**{k: (v.pk if hasattr(v, "pk") else v) for k, v in fields.items()},
                                             **{k: v for k, v in d.items() if k in (
                                                 "name", "percent", "fixed_amount", "fixed_per", "base", "basis",
                                                 "note")},
                                             "valid_from": start.isoformat(), "valid_to": None},
                                       context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            new = ser.save()
            if old.valid_to is None or old.valid_to >= start:
                old.valid_to = start - timedelta(days=1)
                old.save(update_fields=["valid_to", "updated_at"])
            audit.log_event("create", new, reason=gettext('Новая версия правила №{pk}: {reason}').format(pk=old.pk, reason=reason), user=request.user,
                            changes={"previous": [None, old.pk], "valid_from": [None, start.isoformat()]})
        return Response(self.get_serializer(new).data, status=201)

    @action(detail=False, methods=["get"])
    def preview(self, request):
        """Сколько получит франчайзи и какое правило сработало."""
        from datetime import date as _date

        from django.utils.dateparse import parse_date
        from decimal import Decimal, InvalidOperation
        p = request.query_params
        user = request.user
        fid = user.franchisee_id or (int(p["franchisee"]) if p.get("franchisee", "").isdigit() else None)
        product = Product.objects.filter(pk=int(p["product"])).first() if p.get("product", "").isdigit() else (
            Product.objects.filter(code=p["product"]).first() if p.get("product") else None)
        tariff = Tariff.objects.filter(pk=int(p["tariff"])).first() if p.get("tariff", "").isdigit() else None
        kind = p.get("sale_kind") or "subscription"
        if kind not in ("subscription", "setup", "training", "equipment", "other", "renewal"):
            raise BusinessError(gettext("Неизвестный вид продажи."))
        try:
            amount = Decimal(p.get("amount") or "0")
            list_amount = Decimal(p["list_amount"]) if p.get("list_amount") else None
            months = int(p["months"]) if p.get("months") else None
            on = parse_date(p["date"]) if p.get("date") else today()
        except (InvalidOperation, ValueError):
            raise BusinessError(gettext("Неверные параметры расчёта."))
        if not isinstance(on, _date):
            raise BusinessError(gettext("Неверная дата."))
        partner = Franchisee.objects.filter(pk=fid).first() if fid else None
        tier = partner_svc.tier_on(partner, on) if partner else (p.get("partner_tier") or "")
        rule, calc = rule_preview(franchisee_id=fid, product_id=product.id if product else None,
                                  tariff_id=tariff.id if tariff else None, kind=kind, amount=amount, months=months,
                                  on=on, list_amount=list_amount, partner_tier=tier,
                                  lead_source=p.get("lead_source", ""))
        return Response({"rule": CommissionRuleSerializer(rule, context=self.get_serializer_context()).data
                         if rule else None, "percent": money(calc["percent"]), "fixed": money(calc["fixed"]),
                         "income": money(calc["income"]), "base_income": money(calc["base_income"]),
                         "adjustment_rule": CommissionRuleSerializer(
                             calc["adjustment_rule"], context=self.get_serializer_context()).data
                         if calc["adjustment_rule"] else None,
                         "adjustment_amount": money(calc["adjustment_amount"]),
                         "nbs_share": money(calc["nbs_share"]), "partner_tier": tier})


class SetupSplitRuleViewSet(viewsets.ModelViewSet):
    """setup-split-rules/: распределение дохода за установку (OnAI 20 000 = 12 000 + 8 000); пишет super_admin."""

    resource = "setup_split_rules"
    permission_classes = [RolePermission]
    queryset = __import__("network.models", fromlist=["SetupSplitRule"]).SetupSplitRule.objects.select_related("product")
    serializer_class = __import__("network.serializers", fromlist=["SetupSplitRuleSerializer"]).SetupSplitRuleSerializer
    filterset_class = build_filterset(__import__("network.models", fromlist=["SetupSplitRule"]).SetupSplitRule,
                                      bools=("is_active",), product=product_filter())
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

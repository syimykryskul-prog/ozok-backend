from django.utils.translation import gettext
import re

from django.db import transaction
from django.db.models import Sum
from rest_framework import serializers

from core.l10n import BilingualMixin, L10nField

from accounts.models import User
from billing.models import Charge, FranchiseePayment
from billing.services import finance_map
from clients.reports import month_commission_map, sales_by_product_bulk
from core.exceptions import BusinessError
from core.settings_registry import get_decimal
from core.models import money, month_first, month_last, q2, today
from core.permissions import ADMIN, FINANCE, LIMITED_PARTNER_ROLES, SUPER_ADMIN
from core.serializers import OwnedSerializer

from .commission import min_current_price, price_on, create_tariff, set_price
from .models import (CommissionRule, Contract, Equipment, FranchiseOffer, Franchisee, Package, PartnerStatusHistory,
                     Product, Tariff)
from . import partners as partner_svc
from .services import (busy_products, check_parts, connected_product_ids, contract_products, parts_progress,
                       preset_parts, price_quote)

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def cached(serializer, key, fn):
    cache = serializer.context.setdefault("_cache", {})
    if key not in cache:
        cache[key] = fn()
    return cache[key]


class ProductSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name', 'sphere', 'description')

    connected = serializers.SerializerMethodField()
    events_24h = serializers.SerializerMethodField()
    subscription_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ("id", "code", "name", "sphere", "description", "icon", "color", "price_cash", "price_per_sale",
                  "setup_price", "training_price", "min_setup_price", "min_training_price", "max_discount_percent",
                  "grace_days", "restrict_after_days", "payment_source", "subscription_price",
                  "default_commission_percent", "sort_order", "is_active", "integration_enabled", "api_key",
                  "last_event_at", "events_24h", "connected")
        read_only_fields = ("api_key", "last_event_at")
        extra_kwargs = {"color": {"required": False, "allow_blank": True}}

    def get_connected(self, obj):
        user = self.context["request"].user
        if not user.franchisee_id:
            return None
        ids = cached(self, "connected", lambda: connected_product_ids(user.franchisee_id))
        return obj.id in ids

    def get_subscription_price(self, obj):
        """«От N сом» — минимальная текущая цена активных тарифов."""
        price = min_current_price(obj)
        return money(price) if price is not None else None

    def get_events_24h(self, obj):
        """Счётчики событий интеграции за 24 часа (только hq_admin)."""
        if self.context["request"].user.role != ADMIN:
            return None
        from datetime import timedelta
        from django.db.models import Count
        from django.utils import timezone
        from integrations.models import IntegrationEvent

        def load():
            counts = {}
            qs = IntegrationEvent.objects.filter(received_at__gte=timezone.now() - timedelta(hours=24))
            for r in qs.values("product_id", "status").annotate(n=Count("id")):
                counts.setdefault(r["product_id"], {})[r["status"]] = r["n"]
            return counts
        c = cached(self, "events24", load).get(obj.id, {})
        return {k: c.get(k, 0) for k in ("processed", "duplicate", "unmatched", "error")}

    def validate_code(self, value):
        if self.instance and value != self.instance.code:
            raise serializers.ValidationError(gettext("Код сферы нельзя менять после создания."))
        return value

    def validate_color(self, value):
        if value and not HEX.match(value):
            raise serializers.ValidationError(gettext("Цвет задаётся в формате #RRGGBB."))
        return value

    def validate(self, attrs):
        get = lambda k: attrs[k] if k in attrs else getattr(self.instance, k, None)  # noqa: E731
        # M1: минимальная цена не выше рекомендуемой (установка, обучение)
        for floor, price in (("min_setup_price", "setup_price"), ("min_training_price", "training_price")):
            if get(floor) is not None and get(price) is not None and get(floor) > get(price):
                raise serializers.ValidationError({floor: [gettext("Минимальная цена не может быть выше рекомендуемой.")]})
        return attrs

    def validate_default_commission_percent(self, value):
        if not 0 <= value <= 100:
            raise serializers.ValidationError(gettext("Процент должен быть от 0 до 100."))
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.context["request"].user.role != ADMIN:
            data.pop("api_key", None)
            data.pop("events_24h", None)
        return data


class PackageSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name',)

    products = serializers.PrimaryKeyRelatedField(many=True, queryset=Product.objects.all())
    product_codes = serializers.SerializerMethodField()
    connected = serializers.SerializerMethodField()

    class Meta:
        model = Package
        fields = ("id", "name", "products", "product_codes", "price_cash", "price_prepay", "price_per_sale",
                  "prepay_percent", "is_active", "connected")

    def get_product_codes(self, obj):
        return [p.code for p in obj.products.all()]

    def get_connected(self, obj):
        user = self.context["request"].user
        if not user.franchisee_id:
            return None
        ids = cached(self, "connected", lambda: connected_product_ids(user.franchisee_id))
        prods = {p.id for p in obj.products.all()}
        return bool(prods) and prods <= ids

    def validate_products(self, value):
        if not value:
            raise serializers.ValidationError(gettext("Выберите хотя бы одну сферу."))
        return value


class FranchiseeSerializer(serializers.ModelSerializer):
    curator = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(franchisee__isnull=True),
                                                 required=False, allow_null=True)
    recruited_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(franchisee__isnull=True),
                                                      required=False, allow_null=True)
    responsible = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(franchisee__isnull=True),
                                                     required=False, allow_null=True)
    specialization = serializers.PrimaryKeyRelatedField(many=True, queryset=Product.objects.all(), required=False)
    curator_name = serializers.SerializerMethodField()
    recruited_by_name = serializers.CharField(source="recruited_by.full_name", read_only=True, default=None)
    responsible_name = serializers.CharField(source="responsible.full_name", read_only=True, default=None)
    days_in_stage = serializers.SerializerMethodField()
    recruiter_reward = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    overdue_amount = serializers.SerializerMethodField()
    commission_due = serializers.SerializerMethodField()
    commission_month = serializers.SerializerMethodField()
    sales_by_product = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()
    place = serializers.SerializerMethodField()
    employees_count = serializers.SerializerMethodField()
    clients_count = serializers.SerializerMethodField()
    products = serializers.SerializerMethodField()

    class Meta:
        model = Franchisee
        fields = ("id", "name", "partner_code", "legal_name", "full_name", "inn", "passport", "city", "region",
                  "cities", "address", "phone", "email", "owner_name", "specialization", "candidate_source",
                  "recruited_by", "recruited_by_name", "responsible", "responsible_name", "status", "tier",
                  "funnel_stage", "funnel_stage_since", "days_in_stage", "training_date", "certification_result",
                  "certification_date", "suspended_at", "closed_at", "status_reason", "recruiter_reward",
                  "curator", "curator_name", "joined_at", "note", "balance", "overdue_amount", "commission_due",
                  "commission_month", "sales_by_product", "rating", "place", "employees_count", "clients_count",
                  "products")
        read_only_fields = ("funnel_stage_since", "suspended_at", "closed_at")

    def get_days_in_stage(self, obj):
        return partner_svc.days_in_stage(obj)

    def get_recruiter_reward(self, obj):
        reward = getattr(obj, "recruiter_reward", None)
        if reward is None:
            return None
        return {"id": reward.id, "status": reward.status, "accrued_amount": str(reward.accrued_amount),
                "target_amount": str(reward.target_amount)}

    # --- вычисляемые поля (карты считаются один раз на запрос) ---
    def _fin(self, obj):
        return cached(self, "fin", lambda: finance_map(None))

    def _rating(self, obj):
        from planning.services import rating_map
        return cached(self, "rating", lambda: rating_map(month_first(today()))).get(obj.id)

    def get_curator_name(self, obj):
        return obj.curator.full_name if obj.curator_id else None

    def get_balance(self, obj):
        return money(self._fin(obj).get(obj.id, {}).get("balance"))

    def get_overdue_amount(self, obj):
        return money(self._fin(obj).get(obj.id, {}).get("overdue"))

    def get_commission_due(self, obj):
        return money(self._fin(obj).get(obj.id, {}).get("commission_due"))

    def get_commission_month(self, obj):
        first = month_first(today())
        m = cached(self, "comm_month", lambda: month_commission_map(first, month_last(first)))
        return money(m.get(obj.id))

    def get_sales_by_product(self, obj):
        first = month_first(today())
        ids = cached(self, "all_ids", lambda: list(Franchisee.objects.values_list("id", flat=True)))
        m = cached(self, "sbp", lambda: sales_by_product_bulk(first, month_last(first), ids))
        return m.get(obj.id, [])

    def get_rating(self, obj):
        r = self._rating(obj)
        return r["rating"] if r else None

    def get_place(self, obj):
        r = self._rating(obj)
        return r["place"] if r else None

    def get_employees_count(self, obj):
        n = getattr(obj, "employees_n", None)
        return n if n is not None else obj.users.filter(is_active=True).count()

    def get_clients_count(self, obj):
        n = getattr(obj, "clients_n", None)
        return n if n is not None else obj.clients.filter(is_active=True).count()

    def get_products(self, obj):
        contracts = getattr(obj, "active_contracts", None)
        if contracts is None:
            contracts = obj.contracts.filter(status="active").prefetch_related("products")
        return sorted({p.code for c in contracts for p in c.products.all()})

    def to_representation(self, instance):
        data = super().to_representation(instance)
        role = self.context["request"].user.role
        if role in LIMITED_PARTNER_ROLES:  # сотрудник/менеджер не видят долги
            for key in ("balance", "overdue_amount", "commission_due"):
                data[key] = None
        if role not in (SUPER_ADMIN, FINANCE):  # защищённые персональные данные
            for key in Franchisee.PROTECTED_FIELDS:
                data[key] = "***" if data.get(key) else ""
        if role in ("recruiter", "regional_support", "quality", "implementer"):  # без финансов
            for key in ("balance", "overdue_amount", "commission_due", "commission_month", "sales_by_product"):
                data[key] = None
        return data

    def validate_recruited_by(self, value):
        return value

    def validate_partner_code(self, value):
        return (value or "").strip() or None

    def validate_cities(self, value):
        if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
            raise serializers.ValidationError(gettext("Города покрытия — список строк."))
        return [c.strip() for c in value if c.strip()]

    def _strip_protected(self, validated_data):
        if self.context["request"].user.role not in (SUPER_ADMIN, FINANCE):
            for key in Franchisee.PROTECTED_FIELDS:
                validated_data.pop(key, None)

    @transaction.atomic
    def create(self, validated_data):
        self._strip_protected(validated_data)
        user = self.context["request"].user
        spec = validated_data.pop("specialization", [])
        if not validated_data.get("cities") and validated_data.get("city"):
            validated_data["cities"] = [validated_data["city"]]
        stage = validated_data.get("funnel_stage")
        if stage is None and validated_data.get("status"):
            validated_data["funnel_stage"] = {v: k for k, v in reversed(list(Franchisee.STAGE_STATUS.items()))}.get(
                validated_data["status"], "new_application")
        if not validated_data.get("responsible") and user.franchisee_id is None and user.role != "recruiter":
            validated_data["responsible"] = user
        obj = super().create(validated_data)
        if spec:
            obj.specialization.set(spec)
        return obj

    @transaction.atomic
    def update(self, instance, validated_data):
        self._strip_protected(validated_data)
        user = self.context["request"].user
        spec = validated_data.pop("specialization", None)
        status = validated_data.pop("status", None)
        tier = validated_data.pop("tier", None)
        stage = validated_data.pop("funnel_stage", None)
        cert = validated_data.pop("certification_result", None)
        reason = validated_data.get("status_reason", "")
        if instance.status == Franchisee.Status.CLOSED and (
                (status and status != Franchisee.Status.CLOSED) or tier or stage or cert):
            raise serializers.ValidationError(
                gettext("Закрытого партнёра нельзя вернуть через PATCH: используйте franchisees/{id}/reopen/ (Super Admin)."))
        instance = super().update(instance, validated_data)
        if spec is not None:
            instance.specialization.set(spec)
        if stage and stage != instance.funnel_stage:
            partner_svc.set_stage(instance, stage, user=user, reason=reason)
        if status and status != instance.status:
            partner_svc.change_partner_field(instance, "status", status, user=user, reason=reason)
        if tier and tier != instance.tier:
            partner_svc.change_partner_field(instance, "tier", tier, user=user, reason=reason)
        if cert and cert != instance.certification_result:
            partner_svc.record_certification(instance, cert, user=user)
        instance.refresh_from_db()
        return instance


PART_FIELDS = ("upfront_amount", "installments_total", "installments_count")


class ContractSerializer(OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    product_name = L10nField(source="product.name", read_only=True, default=None)
    package_name = L10nField(source="package.name", read_only=True, default=None)
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), required=False, allow_null=True)
    package = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all(), required=False, allow_null=True)
    products = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    product_codes = serializers.SerializerMethodField()
    paid_off_amount = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    charges_total = serializers.SerializerMethodField()
    payoff = serializers.SerializerMethodField()
    withhold_target = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Contract
        fields = ("id", "number", "franchisee", "franchisee_name", "signed_at", "product", "product_name",
                  "package", "package_name", "products", "product_codes", "payment_scheme", "list_price",
                  "fee_total", "discount_note", "prepay_percent", "upfront_amount", "upfront_due",
                  "installments_total", "installments_count", "installment_step_months", "withhold_percent",
                  "offer", "withhold_target", "status", "valid_until", "note", "paid_off_amount", "remaining_amount",
                  "payoff", "charges_total", "created_at")
        read_only_fields = ("number", "status", "list_price")

    FROZEN_OK = {"note", "valid_until", "withhold_percent", "discount_note"}

    def get_product_codes(self, obj):
        return [p.code for p in obj.products.all()]

    def _sums(self, obj):
        def load():
            data = {}
            for r in FranchiseePayment.objects.filter(contract__isnull=False).values("contract_id", "method") \
                    .annotate(s=Sum("amount")):
                d = data.setdefault(r["contract_id"], [0, 0])
                d[1 if r["method"] == "offset" else 0] += r["s"]
            return data
        return cached(self, "paid", load).get(obj.id, [0, 0])

    def get_paid_off_amount(self, obj):
        manual, offsets = self._sums(obj)
        return money(q2(manual) + q2(offsets))

    def get_remaining_amount(self, obj):
        manual, offsets = self._sums(obj)
        return money(max(q2(obj.fee_total) - q2(manual) - q2(offsets), 0))

    def get_payoff(self, obj):
        parts = parts_progress(obj, *self._sums(obj))
        return {k: {"paid": money(v["paid"]), "total": money(v["total"])} for k, v in parts.items()}

    def get_charges_total(self, obj):
        m = cached(self, "charges", lambda: {r["contract_id"]: r["s"] for r in Charge.objects.filter(
            contract__isnull=False).values("contract_id").annotate(s=Sum("amount"))})
        return money(m.get(obj.id))

    def validate_prepay_percent(self, value):
        if not 0 < value <= 100:
            raise serializers.ValidationError(gettext("Предоплата — от 0 (не включая) до 100 процентов."))
        return value

    def validate_installments_count(self, value):
        if value < 0:
            raise serializers.ValidationError(gettext("Количество платежей не может быть отрицательным."))
        return value

    def validate_installment_step_months(self, value):
        if value < 1:
            raise serializers.ValidationError(gettext("Шаг рассрочки — не меньше одного месяца."))
        return value

    def validate_fee_total(self, value):
        if value < 0:
            raise serializers.ValidationError(gettext("Сумма взноса не может быть отрицательной."))
        return value

    def validate_withhold_percent(self, value):
        if not 0 <= value <= 100:
            raise serializers.ValidationError(gettext("Удержание — от 0 до 100 процентов."))
        return value

    def validate(self, attrs):
        inst = self.instance
        if inst is not None and inst.status != Contract.Status.DRAFT:
            extra = set(attrs) - self.FROZEN_OK - {"franchisee"}
            if extra:
                raise serializers.ValidationError(gettext("Изменить можно только черновик договора "
                                                  "(у действующего — заметку, срок, удержание и основание скидки)."))
        offer = attrs.get("offer")
        if offer is not None and inst is None:
            if not offer.is_active:
                raise serializers.ValidationError({"offer": [gettext("Предложение отключено.")]})
            attrs.setdefault("fee_total", offer.total)
            attrs.setdefault("upfront_amount", offer.upfront)
            attrs.setdefault("installments_total", offer.total - offer.upfront)
            attrs.setdefault("installments_count", offer.installments_count)
            attrs.setdefault("installment_step_months", offer.step_months)
            if offer.installments_count == 0 and offer.upfront < offer.total:
                pass
        # ровно одно из product/package
        if "product" in attrs and attrs["product"] and "package" not in attrs:
            attrs["package"] = None
        if "package" in attrs and attrs["package"] and "product" not in attrs:
            attrs["product"] = None
        product = attrs["product"] if "product" in attrs else (inst.product if inst else None)
        package = attrs["package"] if "package" in attrs else (inst.package if inst else None)
        if (product is None) == (package is None):
            raise serializers.ValidationError(gettext("Укажите сферу или пакет (одно из двух)."))
        attrs = super().validate(attrs)
        repriced = inst is None or {"product", "package", "payment_scheme"} & set(attrs)
        if repriced:
            for obj, label in ((product, gettext("Сфера отключена.")), (package, gettext("Пакет отключён."))):
                if obj is not None and not obj.is_active:
                    raise serializers.ValidationError(label)
            scheme = attrs.get("payment_scheme", inst.payment_scheme if inst else Contract.Scheme.CASH)
            try:
                quote = price_quote(product=product, package=package, scheme=scheme)
            except BusinessError as e:
                raise serializers.ValidationError({"payment_scheme": [str(e.detail)]})
            attrs["list_price"] = quote["list_price"]
            attrs.setdefault("fee_total", quote["list_price"])
            if scheme == "prepay" and package is not None and "prepay_percent" not in attrs:
                attrs["prepay_percent"] = quote["prepay_percent"]
            fr = attrs.get("franchisee") or (inst.franchisee if inst else None)
            prods = [product] if product else list(package.products.all())
            busy = busy_products(fr, prods, exclude_contract=inst)
            if busy and (inst is None or inst.status == Contract.Status.ACTIVE):
                raise serializers.ValidationError(
                    gettext("У франчайзи уже есть действующий договор на сферу: ") + ", ".join(p.name for p in busy) + ".")
        # пресет заполняет части, пока их не задали явно; центр потом может изменить любую часть
        if repriced and not (set(PART_FIELDS) & set(attrs)):
            fee = attrs.get("fee_total", inst.fee_total if inst else 0)
            pct = attrs.get("prepay_percent", inst.prepay_percent if inst else 40)
            scheme = attrs.get("payment_scheme", inst.payment_scheme if inst else Contract.Scheme.CASH)
            attrs.update(preset_parts(scheme, fee, pct, attrs.get("installments_count") or 3))
        merged = {k: attrs.get(k, getattr(inst, k) if inst else d) for k, d in (
            ("fee_total", 0), ("upfront_amount", 0), ("installments_total", 0), ("installments_count", 0),
            ("withhold_percent", get_decimal("franchise.default_withhold_percent")))}
        try:
            check_parts(merged["fee_total"], merged["upfront_amount"], merged["installments_total"],
                        merged["installments_count"], merged["withhold_percent"])
        except BusinessError as e:
            raise serializers.ValidationError(str(e.detail))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        contract = super().create(validated_data)
        contract.products.set(contract_products(contract))
        return contract

    @transaction.atomic
    def update(self, instance, validated_data):
        contract = super().update(instance, validated_data)
        if {"product", "package"} & set(validated_data):
            contract.products.set(contract_products(contract))
        return contract


class TariffPriceSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    monthly_price = serializers.DecimalField(max_digits=14, decimal_places=2)
    valid_from = serializers.DateField()
    valid_to = serializers.DateField(read_only=True)


class TariffSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name', 'description')

    product_code = serializers.CharField(source="product.code", read_only=True)
    product_name = L10nField(source="product.name", read_only=True)
    current_price = serializers.SerializerMethodField()
    prices = serializers.SerializerMethodField()
    monthly_price = serializers.DecimalField(max_digits=14, decimal_places=2, write_only=True, required=False,
                                             min_value=0)

    class Meta:
        model = Tariff
        fields = ("id", "product", "product_code", "product_name", "code", "name", "description", "is_active",
                  "sort_order", "min_price", "max_discount_percent", "grace_days", "restrict_after_days",
                  "current_price", "prices", "monthly_price")
        validators = []

    def get_current_price(self, obj):
        price = price_on(obj)
        return money(price) if price is not None else None

    def get_prices(self, obj):
        return [{"id": p.id, "monthly_price": money(p.monthly_price), "valid_from": p.valid_from.isoformat(),
                 "valid_to": p.valid_to.isoformat() if p.valid_to else None} for p in obj.prices.all()]

    def validate(self, attrs):
        if self.instance is not None:
            attrs.pop("product", None)
            attrs.pop("monthly_price", None)
            if "code" in attrs and attrs["code"] != self.instance.code:
                raise serializers.ValidationError({"code": [gettext("Код тарифа нельзя менять: его присылает интеграция.")]})
        else:
            if "monthly_price" not in attrs:
                raise serializers.ValidationError({"monthly_price": [gettext("Укажите цену в месяц.")]})
            if Tariff.objects.filter(product=attrs["product"], code=attrs["code"]).exists():
                raise serializers.ValidationError({"code": [gettext("Тариф с таким кодом уже есть у продукта.")]})
        return attrs

    def create(self, validated_data):
        price = validated_data.pop("monthly_price")
        product = validated_data.pop("product")
        return create_tariff(product=product, monthly_price=price, **validated_data)


class SetPriceSerializer(serializers.Serializer):
    monthly_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    valid_from = serializers.DateField()


class EquipmentSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name',)

    product_name = L10nField(source="product.name", read_only=True, default=None)

    class Meta:
        model = Equipment
        fields = ("id", "name", "product", "product_name", "price", "sale_price", "min_price", "purchase_price",
                  "margin", "margin_percent", "warranty_months", "unit", "is_active", "sort_order")

    sale_price = serializers.DecimalField(max_digits=14, decimal_places=2, source="price", required=False)
    margin = serializers.SerializerMethodField()
    margin_percent = serializers.SerializerMethodField()

    def get_margin(self, obj):
        return money(obj.margin)

    def get_margin_percent(self, obj):
        return money(obj.margin * 100 / obj.price) if obj.price else None

    def validate(self, attrs):
        get = lambda k: attrs[k] if k in attrs else getattr(self.instance, k, None)  # noqa: E731
        for k in ("price", "purchase_price"):
            if get(k) is not None and get(k) < 0:
                raise serializers.ValidationError({k: [gettext("Цена не может быть отрицательной.")]})
        if get("min_price") is not None and get("price") is not None and get("min_price") > get("price"):
            raise serializers.ValidationError({"min_price": [gettext("Минимальная цена не может быть выше цены продажи.")]})
        return attrs


RULE_ECONOMIC_FIELDS = ("percent", "fixed_amount", "fixed_per", "base", "product", "tariff", "franchisee",
                        "sale_kind", "partner_tier", "lead_source", "valid_from")


def rule_is_used(rule):
    """Правило применялось: на него ссылаются продажи или начисления комиссий."""
    from billing.models import CommissionAccrual
    from clients.models import ClientSale
    return (ClientSale.all_objects.filter(models_q(rule)).exists()
            or CommissionAccrual.all_objects.filter(models_q(rule, "rule", "adjustment_rule")).exists())


def models_q(rule, a="commission_rule", b="adjustment_rule"):
    from django.db.models import Q
    return Q(**{a: rule}) | Q(**{b: rule})


class CommissionRuleSerializer(serializers.ModelSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)
    product_name = L10nField(source="product.name", read_only=True, default=None)
    tariff_name = L10nField(source="tariff.name", read_only=True, default=None)

    class Meta:
        model = CommissionRule
        fields = ("id", "name", "product", "product_name", "tariff", "tariff_name", "franchisee",
                  "franchisee_name", "sale_kind", "partner_tier", "lead_source", "basis", "percent", "fixed_amount",
                  "fixed_per", "base", "valid_from", "valid_to", "is_active", "note")

    def validate(self, attrs):
        get = lambda k: attrs[k] if k in attrs else getattr(self.instance, k, None)  # noqa: E731
        if get("percent") is None and get("fixed_amount") is None:
            raise serializers.ValidationError(gettext("Укажите процент и/или фиксированную сумму."))
        if get("percent") is not None and not 0 <= get("percent") <= 100:
            raise serializers.ValidationError({"percent": [gettext("Процент — от 0 до 100.")]})
        if get("fixed_amount") is not None and get("fixed_amount") < 0:
            raise serializers.ValidationError({"fixed_amount": [gettext("Сумма не может быть отрицательной.")]})
        if get("valid_to") and get("valid_from") and get("valid_to") < get("valid_from"):
            raise serializers.ValidationError({"valid_to": [gettext("Дата окончания раньше даты начала.")]})
        tariff, product = get("tariff"), get("product")
        if tariff and product and tariff.product_id != product.id:
            raise serializers.ValidationError({"tariff": [gettext("Тариф относится к другой сфере.")]})
        if tariff and not product:
            attrs["product"] = tariff.product
        if get("fixed_per") == "month" and get("sale_kind") != "subscription":
            raise serializers.ValidationError({"fixed_per": [gettext("Фикс «за месяц» возможен только для абонплаты.")]})
        if tariff and get("sale_kind") in ("setup", "training", "equipment", "other"):
            raise serializers.ValidationError({"tariff": [gettext("Тариф указывается только для правил абонплаты.")]})
        if get("lead_source") and not (get("basis") or "").strip():
            raise serializers.ValidationError({"basis": [gettext("Для правила по лидам OZOK укажите основание.")]})
        if self.instance is not None:
            reason = str(self.initial_data.get("reason") or "").strip()
            if not reason:
                raise serializers.ValidationError({"reason": [gettext("Укажите причину изменения правила.")]})
            if rule_is_used(self.instance):
                frozen = [f for f in RULE_ECONOMIC_FIELDS if f in attrs and attrs[f] != getattr(self.instance, f)]
                if frozen:
                    raise serializers.ValidationError({
                        "detail": gettext("Правило уже применялось к начислениям: ставки задним числом менять нельзя. "
                                  "Создайте новую версию с датой начала (POST commission-rules/{id}/new-version/)."),
                        "fields": frozen})
                if "valid_to" in attrs and attrs["valid_to"] is not None and attrs["valid_to"] < today():
                    raise serializers.ValidationError({"valid_to": [gettext("Применённое правило нельзя закрыть задним числом.")]})
        if get("lead_source") and get("lead_source") not in __import__("leads.models", fromlist=["Lead"]).Lead.Source.values:
            raise serializers.ValidationError({"lead_source": [gettext("Неизвестный источник лида.")]})
        return attrs


class PartnerStatusHistorySerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    decided_by_name = serializers.CharField(source="decided_by.full_name", read_only=True, default=None)
    old = serializers.CharField(required=False, allow_blank=True, read_only=True)

    class Meta:
        model = PartnerStatusHistory
        fields = ("id", "partner", "partner_name", "field", "old", "new", "effective_from", "reason", "decided_by",
                  "decided_by_name", "applied", "created_at")
        read_only_fields = ("decided_by", "applied", "old")


class FranchiseOfferSerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name',)

    installment_amount = serializers.SerializerMethodField()

    class Meta:
        model = FranchiseOffer
        fields = ("id", "name", "total", "upfront", "installments_count", "step_months", "installment_amount",
                  "is_active", "sort_order")

    def get_installment_amount(self, obj):
        if not obj.installments_count:
            return None
        return money((obj.total - obj.upfront) / obj.installments_count)

    def validate(self, attrs):
        g = lambda k: attrs[k] if k in attrs else getattr(self.instance, k, 0)  # noqa: E731
        if g("upfront") > g("total") or min(g("total"), g("upfront")) < 0:
            raise serializers.ValidationError(gettext("Сумма «сразу» не может превышать стоимость."))
        if g("total") > g("upfront") and not g("installments_count"):
            raise serializers.ValidationError(gettext("Для остатка укажите количество платежей рассрочки."))
        return attrs


from .models import SetupSplitRule  # noqa: E402


class SetupSplitRuleSerializer(serializers.ModelSerializer):
    product_name = L10nField(source="product.name", read_only=True)

    class Meta:
        model = SetupSplitRule
        fields = ("id", "product", "product_name", "total", "partner_amount", "implementer_amount",
                  "implementer_condition", "technical_channels", "valid_from", "valid_to", "is_active")

    def validate(self, attrs):
        get = lambda k: attrs[k] if k in attrs else getattr(self.instance, k, None)  # noqa: E731
        if min(get("total"), get("partner_amount"), get("implementer_amount")) < 0:
            raise serializers.ValidationError(gettext("Суммы не могут быть отрицательными."))
        if q2(get("partner_amount")) + q2(get("implementer_amount")) != q2(get("total")):
            raise serializers.ValidationError(gettext("Доля партнёра + доля внедренца должны равняться стоимости установки."))
        ch = get("technical_channels")
        if not isinstance(ch, list) or not all(isinstance(c, str) for c in ch):
            raise serializers.ValidationError({"technical_channels": [gettext("Список названий каналов.")]})
        return attrs

from django.utils.translation import gettext, gettext_lazy
from rest_framework import serializers

from core.l10n import L10nField

from core.models import money
from core.serializers import ScopedPK
from leads.models import Lead
from network.models import Equipment, Franchisee, Product, Tariff

from . import deals
from .models import Branch, Client, Deal, DealLine

NBS_DIRECT_LABEL = gettext_lazy("OZOK (прямые продажи)")


class DealLineInSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=DealLine.Kind.choices)
    equipment = serializers.PrimaryKeyRelatedField(queryset=Equipment.objects.all(), required=False, allow_null=True)
    tariff = serializers.PrimaryKeyRelatedField(queryset=Tariff.objects.all(), required=False, allow_null=True)
    quantity = serializers.DecimalField(max_digits=10, decimal_places=3, required=False, default=1)
    months = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=120)
    serial_numbers = serializers.ListField(child=serializers.CharField(), required=False)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=0)
    discount_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=0)
    discount_reason = serializers.CharField(required=False, allow_blank=True, default="")


class DealLineSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    equipment_name = L10nField(source="equipment.name", read_only=True, default=None)
    tariff_name = L10nField(source="tariff.name", read_only=True, default=None)
    income_now = serializers.SerializerMethodField()
    income_on_payment = serializers.SerializerMethodField()

    def get_income_now(self, obj):
        """Доход партнёра, который зачисляется сразу (абонплата — 0: только когда клиент платит)."""
        return money(0 if obj.kind == "subscription" else obj.partner_income)

    def get_income_on_payment(self, obj):
        """Ожидаемый доход партнёра с абонплаты в момент оплаты клиентом (для остальных строк — 0)."""
        return money(deals.subscription_income_on_payment(obj) if obj.kind == "subscription" else 0)

    class Meta:
        model = DealLine
        fields = ("id", "kind", "kind_label", "equipment", "equipment_name", "tariff", "tariff_name", "quantity",
                  "months", "serial_numbers", "list_price", "discount_percent", "discount_amount", "discount_reason",
                  "list_amount", "line_discount", "deal_discount_share", "total", "partner_income", "nbs_income",
                  "withheld", "violation", "subscription_status", "sale", "income_now", "income_on_payment")


class NewClientSerializer(serializers.Serializer):
    business_name = serializers.CharField()
    phone = serializers.CharField(required=False, allow_blank=True, default="")
    city = serializers.CharField(required=False, allow_blank=True, default="")
    contact_name = serializers.CharField(required=False, allow_blank=True, default="")


class DealSerializer(serializers.ModelSerializer):
    """Продажа одной формой. Вход: client | new_client, product, date, lines[], deal_discount_*."""

    client = ScopedPK(queryset=Client.objects.all(), required=False, allow_null=True)
    partner = serializers.PrimaryKeyRelatedField(queryset=Franchisee.objects.all(), required=False, allow_null=True)
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    lead = ScopedPK(queryset=Lead.objects.all(), scope="franchisee_id", required=False, allow_null=True)
    lines = DealLineInSerializer(many=True, write_only=True, required=False)
    new_client = NewClientSerializer(write_only=True, required=False)
    branch = ScopedPK(queryset=Branch.objects.all(), scope=None, required=False, allow_null=True)
    items = DealLineSerializer(source="lines", many=True, read_only=True)
    partner_name = serializers.SerializerMethodField()
    client_title = serializers.SerializerMethodField()
    product_name = L10nField(source="product.name", read_only=True)
    is_nbs_direct = serializers.SerializerMethodField()
    change_request = serializers.SerializerMethodField()

    class Meta:
        model = Deal
        fields = ("id", "client", "client_title", "client_name", "new_client", "partner", "partner_name",
                  "is_nbs_direct", "lead", "product", "product_name", "date", "status", "comment",
                  "deal_discount_percent", "deal_discount_amount", "discount_reason", "lines", "items", "list_total",
                  "discount_total", "total", "partner_income_total", "nbs_income_total", "approval_note",
                  "change_request", "approved_by", "approved_at", "confirmed_at", "cancelled_at", "cancel_reason",
                  "created_at", "branch", "price_list_snapshot", "contract_number", "signed_at", "payment_status")
        read_only_fields = ("status", "list_total", "discount_total", "total", "partner_income_total",
                            "nbs_income_total", "approval_note", "approved_by", "approved_at", "confirmed_at",
                            "cancelled_at", "cancel_reason", "price_list_snapshot", "payment_status")

    def get_partner_name(self, obj):
        return obj.partner.name if obj.partner_id else NBS_DIRECT_LABEL

    def get_client_title(self, obj):
        return obj.client.business_name if obj.client_id else obj.client_name

    def get_is_nbs_direct(self, obj):
        return obj.partner_id is None

    def get_change_request(self, obj):
        from core.models import ChangeRequest
        cr = ChangeRequest.objects.filter(entity="clients.Deal", entity_id=obj.pk).order_by("-id").first()
        return {"id": cr.id, "status": cr.status} if cr else None

    def resolve(self, attrs):
        """Единые правила партнёра/клиента для сохранения и предпросмотра."""
        user = self.context["request"].user
        inst = self.instance
        client = attrs.get("client", inst.client if inst else None)
        partner = attrs.get("partner", inst.partner if inst else None)
        if user.franchisee_id:
            partner = user.franchisee
            if client is None and not attrs.get("new_client") and inst is None:
                raise serializers.ValidationError({"client": [gettext("Выберите клиента или создайте нового.")]})
            if "lines" in attrs and any(l["kind"] == "subscription" for l in attrs["lines"]) \
                    and user.role == "manager" and False:
                pass
        else:
            if client is not None:
                partner = client.franchisee
            elif partner is None and not (attrs.get("client_name") or (inst and inst.client_name)):
                raise serializers.ValidationError(
                    {"client_name": [gettext("Для продажи OZOK без партнёра укажите название клиента.")]})
            elif partner is not None and not attrs.get("new_client") and client is None and inst is None:
                raise serializers.ValidationError({"client": [gettext("Укажите клиента партнёра.")]})
        return client, partner

    def validate(self, attrs):
        if self.instance is not None and self.instance.status != Deal.Status.DRAFT:
            raise serializers.ValidationError(gettext("Изменить можно только черновик продажи."))
        if self.instance is None and "lines" not in attrs:
            raise serializers.ValidationError({"lines": [gettext("Добавьте хотя бы одну строку.")]})
        client, partner = self.resolve(attrs)
        branch = attrs.get("branch")
        if branch is not None and (client is None or branch.client_id != client.id):
            raise serializers.ValidationError({"branch": [gettext("Филиал относится к другому клиенту.")]})
        lead = attrs.get("lead")
        if lead is not None:
            if lead.client_id is not None and client is not None and lead.client_id != client.id:
                raise serializers.ValidationError({"lead": [gettext("Заявка относится к другому клиенту.")]})
            if partner is not None and lead.franchisee_id not in (None, partner.id):
                raise serializers.ValidationError({"lead": [gettext("Заявка принадлежит другому партнёру.")]})
            if client is not None and client.lead_id and client.lead_id != lead.id:
                raise serializers.ValidationError({"lead": [gettext("У клиента уже есть другая заявка-источник.")]})
        attrs["_client"], attrs["_partner"] = client, partner
        return attrs

    def _prepare_lines(self, lines):
        return [{k: v for k, v in dict(l).items()} for l in lines]

    def _build(self, attrs):
        user = self.context["request"].user
        new_client = attrs.pop("new_client", None)
        client, partner = attrs.pop("_client"), attrs.pop("_partner")
        if new_client and client is None and partner is not None:
            client = Client.objects.create(franchisee=partner, **new_client)
        attrs["client"], attrs["partner"] = client, partner
        return user, attrs

    def create(self, validated):
        user, attrs = self._build(dict(validated))
        lines = self._prepare_lines(attrs.pop("lines"))
        deal = Deal(created_by=user, **attrs)
        deal, _ = deals.save_deal(deal, lines)
        return deal

    def update(self, instance, validated):
        user, attrs = self._build(dict(validated))
        lines = attrs.pop("lines", None)
        for k, v in attrs.items():
            setattr(instance, k, v)
        if lines is None:
            lines = [{"kind": l.kind, "equipment": l.equipment, "tariff": l.tariff, "quantity": l.quantity,
                      "months": l.months, "serial_numbers": l.serial_numbers, "discount_percent": l.discount_percent,
                      "discount_amount": l.discount_amount, "discount_reason": l.discount_reason}
                     for l in instance.lines.all()]
        else:
            lines = self._prepare_lines(lines)
        deal, _ = deals.save_deal(instance, lines)
        return deal


def preview_payload(computed, *, product, partner):
    """Ответ deals/preview/: по строкам доход партнёра, доля OZOK, удержание, нарушения."""
    t = computed["totals"]
    lines = []
    for r in computed["lines"]:
        lines.append({
            "kind": r["kind"], "list_price": money(r["list_price"]), "quantity": str(r["qty"]),
            "list_amount": money(r["list_amount"]), "line_discount": money(r["line_discount"]),
            "deal_discount_share": money(r["share"]), "total": money(r["total"]),
            "partner_income": money(r["partner_income"]), "nbs_share": money(r["nbs_share"]),
            "withheld": money(r["withheld"]), "partner_net": money(r["partner_net"]),
            "nbs_income": money(r["nbs_income"]), "violation": r["violation"], "note": r.get("note", ""),
            "pays_commission_on_payment": r["kind"] == "subscription"})
    return {"partner": partner.id if partner else None, "is_nbs_direct": partner is None,
            "partner_status": computed["partner_status"], "partner_tier": computed["partner_tier"],
            "product": product.id, "lines": lines,
            "totals": {"list_total": money(t["list_total"]), "discount_total": money(t["discount_total"]),
                       "total": money(t["total"]), "partner_income": money(t["partner_income_total"]),
                       "withheld": money(t["withheld_total"]), "nbs_share": money(t["nbs_share_total"]),
                       "partner_net": money(t["partner_net_total"]), "nbs_income": money(t["nbs_income_total"])},
            "needs_approval": bool(computed["violations"]), "violations": computed["violations"]}

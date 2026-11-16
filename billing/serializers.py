from django.utils.translation import gettext
from rest_framework import serializers

from core.l10n import BilingualMixin, L10nField

from core.models import today
from core.serializers import OwnedSerializer, ScopedPK
from network.models import Contract

from .models import Charge, CommissionPayout, FranchiseePayment


class ChargeSerializer(OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    contract = ScopedPK(queryset=Contract.objects.all(), required=False, allow_null=True)
    overdue = serializers.SerializerMethodField()
    consistency_fields = ("contract",)

    class Meta:
        model = Charge
        fields = ("id", "franchisee", "franchisee_name", "contract", "kind", "amount", "due_date", "description",
                  "overdue", "cancelled_at", "cancel_reason", "created_at")
        read_only_fields = ("cancelled_at", "cancel_reason")

    def get_overdue(self, obj):
        return obj.due_date is not None and obj.due_date < today()

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(gettext("Сумма должна быть больше нуля."))
        return value


class _ManualMoneyMixin:
    """Ручной ввод: способ cash/bank; зачёты (offset) создаёт только система."""

    def validate_method(self, value):
        if value == "offset":
            raise serializers.ValidationError(gettext("Зачёты создаются автоматически при продажах."))
        return value

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(gettext("Сумма должна быть больше нуля."))
        return value

    def validate(self, attrs):
        if self.instance is not None and self.instance.method == "offset":
            raise serializers.ValidationError(gettext("Автоматический зачёт изменить нельзя."))
        return super().validate(attrs)


class FranchiseePaymentSerializer(_ManualMoneyMixin, OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default=None)
    contract = ScopedPK(queryset=Contract.objects.all(), required=False, allow_null=True)
    charge = ScopedPK(queryset=Charge.objects.all(), required=False, allow_null=True)
    consistency_fields = ("contract", "charge")

    class Meta:
        model = FranchiseePayment
        fields = ("id", "franchisee", "franchisee_name", "contract", "charge", "income_category", "client_sale",
                  "amount", "date", "method", "comment", "status", "cancelled_at", "cancel_reason", "created_by",
                  "created_by_name", "created_at", "allow_credit")
        read_only_fields = ("client_sale", "created_by", "status", "cancelled_at", "cancel_reason")

    allow_credit = serializers.BooleanField(write_only=True, required=False, default=False)

    def validate(self, attrs):
        allow_credit = attrs.pop("allow_credit", False)
        attrs = super().validate(attrs)
        if self.instance is None:
            if attrs.get("date") and attrs["date"] > today():
                raise serializers.ValidationError({"date": [gettext("Дата платежа не может быть в будущем.")]})
            contract = attrs.get("contract")
            if contract is not None and attrs.get("method") != "offset":
                from network.services import payment_sums
                manual, offsets = payment_sums(contract)
                remaining = max(contract.fee_total - manual - offsets, 0)
                if attrs["amount"] > remaining:
                    user = self.context["request"].user
                    if not (allow_credit and user.role == "super_admin"):
                        raise serializers.ValidationError({"amount": [
                            gettext('Платёж превышает остаток по договору ({remaining} сом). Переплату (аванс) может провести только Super Admin с флагом allow_credit.').format(remaining=remaining)]})
        return attrs

    def validate_income_category(self, value):
        if value:
            from .models import CashCategory
            if not CashCategory.objects.filter(code=value, direction="in", is_active=True).exists():
                raise serializers.ValidationError(gettext("Неизвестная статья поступлений кассы."))
        return value


class CommissionPayoutSerializer(_ManualMoneyMixin, OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default=None)

    class Meta:
        model = CommissionPayout
        fields = ("id", "franchisee", "franchisee_name", "client_sale", "amount", "date", "method", "comment",
                  "cancelled_at", "cancel_reason", "created_by", "created_by_name", "created_at")
        read_only_fields = ("client_sale", "created_by", "cancelled_at", "cancel_reason")


from accounts.models import User  # noqa: E402
from core.models import q2  # noqa: E402

from .models import LedgerCategory, PartnerLedgerEntry, RecruiterReward  # noqa: E402


class RecruiterRewardSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    partner_status = serializers.CharField(source="partner.status", read_only=True)
    recruiter_name = serializers.CharField(source="recruiter.full_name", read_only=True, default=None)
    remaining = serializers.SerializerMethodField()
    allocations = serializers.SerializerMethodField()

    class Meta:
        model = RecruiterReward
        fields = ("id", "partner", "partner_name", "partner_status", "recruiter", "recruiter_name", "target_amount",
                  "accrued_amount", "remaining", "status", "verified_by", "verified_at", "paid_by", "paid_at",
                  "paid_date", "paid_amount", "payment_ref", "debt_amount", "cancel_reason", "allocations",
                  "created_at")
        read_only_fields = fields

    def get_remaining(self, obj):
        return str(max(obj.target_amount - obj.accrued_amount, 0))

    def get_allocations(self, obj):
        # M7: рекрутёр видит только итоги, без платежей партнёра
        if self.context["request"].user.role == "recruiter":
            return None
        return [{"payment": a.payment_id, "amount": str(a.amount), "date": a.payment.date.isoformat(),
                 "method": a.payment.method} for a in obj.allocations.select_related("payment")]


class RewardCreateSerializer(serializers.Serializer):
    partner = serializers.IntegerField()


class LedgerCategorySerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name',)

    class Meta:
        model = LedgerCategory
        fields = ("id", "type", "name", "is_active", "is_marketing", "sort_order")


class LedgerEntrySerializer(serializers.ModelSerializer):
    category_name = L10nField(source="category.name", read_only=True)
    partner_name = serializers.CharField(source="partner.name", read_only=True)

    class Meta:
        model = PartnerLedgerEntry
        fields = ("id", "partner", "partner_name", "date", "type", "category", "category_name", "amount",
                  "description", "product", "created_by", "cancelled_at", "cancel_reason", "created_at")
        read_only_fields = ("partner", "created_by", "cancelled_at", "cancel_reason")

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(gettext("Сумма должна быть больше нуля."))
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        cat = attrs.get("category") or (self.instance.category if self.instance else None)
        typ = attrs.get("type") or (self.instance.type if self.instance else None)
        if cat is not None:
            if typ and cat.type != typ:
                raise serializers.ValidationError({"category": [gettext("Категория не соответствует типу записи.")]})
            attrs.setdefault("type", cat.type)
            if not cat.is_active and (self.instance is None or "category" in attrs):
                raise serializers.ValidationError({"category": [gettext("Категория отключена.")]})
        if self.instance is None:
            if user.franchisee_id:
                attrs["partner"] = user.franchisee
            else:
                pid = self.initial_data.get("partner") or self.initial_data.get("franchisee")
                from network.models import Franchisee
                partner = Franchisee.objects.filter(pk=pid).first() if str(pid or "").isdigit() else None
                if partner is None:
                    raise serializers.ValidationError({"partner": [gettext("Укажите партнёра.")]})
                attrs["partner"] = partner
        return attrs


# ---- P9 / P8 ------------------------------------------------------------------------------------------------------
from clients.models import Branch, Subscription  # noqa: E402

from .models import CommissionAccrual, Payment, Payout  # noqa: E402


class PaymentSerializer(serializers.ModelSerializer):
    currency = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)
    sale = serializers.IntegerField(source="sale_id", read_only=True)
    commission_amount = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = ("id", "branch", "branch_name", "subscription", "client", "client_name", "product", "product_code",
                  "deal", "sale", "amount", "currency", "refunded_amount", "paid_at", "period_from", "period_to",
                  "months", "source", "external_id", "elqr_account", "purpose", "status", "reconciled_at",
                  "reconciled_source", "commission_amount", "cancelled_at", "cancel_reason", "created_at")
        read_only_fields = fields

    def get_currency(self, obj):
        return "KGS"  # решение заказчика: только сомы

    def get_commission_amount(self, obj):
        from django.db.models import Sum
        v = obj.accruals.aggregate(s=Sum("amount"))["s"]
        return str(v or 0)


class PaymentCreateSerializer(serializers.Serializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())
    subscription = serializers.PrimaryKeyRelatedField(queryset=Subscription.objects.all(), required=False,
                                                      allow_null=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    currency = serializers.CharField(required=False, write_only=True)
    paid_at = serializers.DateTimeField()
    months = serializers.IntegerField(required=False, min_value=1, max_value=120, default=1)
    external_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    source = serializers.ChoiceField(choices=("manual", "bank_import"), default="manual")
    purpose = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    period_from = serializers.DateField(required=False, allow_null=True)
    period_to = serializers.DateField(required=False, allow_null=True)

    def validate_currency(self, value):
        if value.upper() != "KGS":
            raise serializers.ValidationError(gettext("Система работает только в сомах (KGS)."))
        return value


class AccrualSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)
    payment_paid_at = serializers.DateTimeField(source="payment.paid_at", read_only=True, default=None)
    payment_source = serializers.CharField(source="payment.source", read_only=True, default=None)
    net_amount = serializers.SerializerMethodField()
    rule_name = serializers.CharField(source="rule.name", read_only=True, default=None)
    adjustment_rule_name = serializers.CharField(source="adjustment_rule.name", read_only=True, default=None)

    class Meta:
        model = CommissionAccrual
        fields = ("id", "payment", "payment_paid_at", "payment_source", "sale", "partner", "partner_name", "branch",
                  "branch_name", "client", "client_name", "product", "product_code", "date", "rule", "rule_name",
                  "adjustment_rule", "adjustment_rule_name", "rate", "fixed_amount", "base_amount",
                  "adjustment_amount", "partner_tier", "lead_source", "amount", "withheld_amount", "net_amount",
                  "status", "reversal_of", "reason", "payout", "cancelled_at", "cancel_reason", "created_at")
        read_only_fields = fields

    def get_net_amount(self, obj):
        return str(obj.amount - obj.withheld_amount)


class PayoutSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    accruals = serializers.SerializerMethodField()
    verified_by_name = serializers.CharField(source="verified_by.full_name", read_only=True, default=None)
    paid_by_name = serializers.CharField(source="paid_by.full_name", read_only=True, default=None)

    class Meta:
        model = Payout
        fields = ("id", "partner", "partner_name", "period", "amount", "status", "verified_by", "verified_by_name",
                  "verified_at", "paid_by", "paid_by_name", "paid_at", "paid_date", "payment_ref", "comment",
                  "accruals", "cancelled_at", "cancel_reason", "created_at")
        read_only_fields = fields

    def get_accruals(self, obj):
        return [{"id": a.id, "date": a.date.isoformat(), "amount": str(a.amount), "withheld": str(a.withheld_amount),
                 "net": str(a.amount - a.withheld_amount), "reason": a.reason, "reversal_of": a.reversal_of_id,
                 "branch": a.branch_id} for a in obj.accruals.all()]


# ---- Касса -------------------------------------------------------------------------------------------------------
from . import cash as cash_svc  # noqa: E402
from .models import CashAccount, CashCategory, CashTransaction, NbsExpense  # noqa: E402


class CashAccountSerializer(serializers.ModelSerializer):
    balance = serializers.SerializerMethodField()

    class Meta:
        model = CashAccount
        fields = ("id", "name", "kind", "is_active", "opening_balance", "opening_date", "balance")

    def get_balance(self, obj):
        return str(cash_svc.balance(obj))


class CashCategorySerializer(BilingualMixin, serializers.ModelSerializer):
    bilingual = ('name',)

    class Meta:
        model = CashCategory
        fields = ("id", "direction", "code", "name", "is_system", "manual_allowed", "is_active", "sort_order")
        read_only_fields = ("is_system",)

    def validate(self, attrs):
        if self.instance is not None and self.instance.is_system:
            attrs.pop("code", None)
            attrs.pop("direction", None)
            attrs.pop("manual_allowed", None)
        return attrs


class CashTransactionSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="account.name", read_only=True)
    transfer_account_name = serializers.CharField(source="transfer_account.name", read_only=True, default=None)
    category_name = L10nField(source="category.name", read_only=True, default=None)
    category_code = serializers.CharField(source="category.code", read_only=True, default=None)
    partner_name = serializers.CharField(source="partner.name", read_only=True, default=None)
    is_reversed = serializers.SerializerMethodField()
    reversal = serializers.SerializerMethodField()

    class Meta:
        model = CashTransaction
        fields = ("id", "date", "account", "account_name", "transfer_account", "transfer_account_name", "direction",
                  "amount", "category", "category_name", "category_code", "partner", "partner_name", "client",
                  "source_type", "source_id", "reversal_of", "is_reversed", "reversal", "comment", "attachment",
                  "is_auto", "created_by", "created_at")
        read_only_fields = ("source_type", "source_id", "reversal_of", "is_auto", "created_by", "client")

    def get_is_reversed(self, obj):
        return cash_svc.T.objects.filter(reversal_of=obj).exists()

    def get_reversal(self, obj):
        r = cash_svc.T.objects.filter(reversal_of=obj).first()
        return r.id if r else None


class NbsExpenseSerializer(serializers.ModelSerializer):
    transaction = CashTransactionSerializer(read_only=True)

    class Meta:
        model = NbsExpense
        fields = ("id", "transaction", "description", "supplier", "created_at")

from django.utils.translation import gettext, gettext_lazy
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from rest_framework import serializers

from core import reference_ky
from core.l10n import L10nField, loc_text

from accounts.models import User
from core.models import money, q2, today
from core.permissions import ACCOUNTANT, ADMIN
from core.serializers import ClientOwnedSerializer, OwnedSerializer, ScopedPK
from network.models import Equipment, Product, Tariff

from . import branches as branch_svc
from . import services
from .models import (Branch, BranchAssignmentHistory, Client, ClientContact, ClientProductAccount, ClientSale,
                     Implementation, Subscription)

RENEWAL_DAYS = 14


def account_flags(account, on=None):
    on = on or today()
    active = account.status in Subscription.LIVE_STATUSES and account.paid_until is not None
    return (active and on <= account.paid_until <= on + timedelta(days=RENEWAL_DAYS),
            active and account.paid_until < on)


class AccountBriefSerializer(serializers.ModelSerializer):
    product_name = L10nField(source="product.name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)

    class Meta:
        model = ClientProductAccount
        fields = ("id", "branch", "product", "product_code", "product_name", "external_id", "status", "paid_until")


class ClientProductAccountSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    franchisee = serializers.IntegerField(source="client.franchisee_id", read_only=True)
    franchisee_name = serializers.CharField(source="client.franchisee.name", read_only=True, default=None)
    product_name = L10nField(source="product.name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)
    sphere = L10nField(source="product.sphere", read_only=True)
    tariff_name = L10nField(source="tariff.name", read_only=True, default=None)
    renewal_soon = serializers.SerializerMethodField()
    expired = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    monthly_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = ClientProductAccount
        fields = ("id", "client", "client_name", "franchisee", "franchisee_name", "product", "product_code",
                  "product_name", "sphere", "external_id", "status", "paid_until", "monthly_fee", "connected_at",
                  "tariff", "tariff_name", "discount_percent", "renewal_soon", "expired", "branch", "branch_name",
                  "monthly_price", "started_at", "closed_at", "deal")

    def get_renewal_soon(self, obj):
        return bool(account_flags(obj)[0])

    def get_expired(self, obj):
        return bool(account_flags(obj)[1])


class ClientSerializer(OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)
    products = serializers.SerializerMethodField()
    last_paid_until = serializers.SerializerMethodField()
    implementations_count = serializers.SerializerMethodField()
    accounts = serializers.SerializerMethodField()
    branches = serializers.SerializerMethodField()
    contacts = serializers.SerializerMethodField()
    renewal_soon = serializers.SerializerMethodField()
    expired = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = ("id", "franchisee", "franchisee_name", "origin", "owner_partner", "business_name", "legal_name", "inn",
                  "region", "business_type", "contact_name", "phone", "city", "address", "lead", "note", "is_active",
                  "products", "last_paid_until", "implementations_count", "accounts", "branches", "contacts",
                  "renewal_soon", "expired", "created_at")
        read_only_fields = ("owner_partner", "origin")

    def validate_lead(self, value):
        if self.context["request"].user.franchisee_id:
            raise serializers.ValidationError(gettext("Лид клиента меняет только OZOK."))
        return value

    def _visible_accounts(self, obj):
        """C9: партнёр видит подписки только тех филиалов, которые обслуживает (владелец клиента — все свои)."""
        user = self.context["request"].user
        accounts = list(obj.accounts.all())
        if not user.franchisee_id:
            return accounts
        served = {b.id for b in obj.branches.all() if branch_svc.visible_to(b, user.franchisee_id)}
        return [a for a in accounts if a.branch_id in served]

    def get_accounts(self, obj):
        return AccountBriefSerializer(self._visible_accounts(obj), many=True, context=self.context).data

    def get_branches(self, obj):
        user = self.context["request"].user
        rows = list(obj.branches.all())
        if user.franchisee_id:
            rows = [b for b in rows if branch_svc.visible_to(b, user.franchisee_id)]
        return [{"id": b.id, "name": b.name, "is_main": b.is_main, "status": b.status,
                 "product": b.product_id, "elqr_account": b.elqr_account} for b in rows]

    def get_contacts(self, obj):
        return [{"id": c.id, "full_name": c.full_name, "position": c.position, "phone": c.phone, "email": c.email,
                 "is_primary": c.is_primary, "branch": c.branch_id} for c in obj.contacts.all()]

    def get_products(self, obj):
        codes = {a.product.code for a in self._visible_accounts(obj)}
        codes |= {s.product.code for s in obj.sales.all()}
        codes |= {i.product.code for i in obj.implementations.all()}
        return sorted(codes)

    def get_last_paid_until(self, obj):
        dates = [a.paid_until for a in self._visible_accounts(obj)
                 if a.paid_until and a.status in Subscription.LIVE_STATUSES]
        return max(dates) if dates else None

    def get_implementations_count(self, obj):
        return len(obj.implementations.all())

    def get_renewal_soon(self, obj):
        return any(account_flags(a)[0] for a in self._visible_accounts(obj))

    def get_expired(self, obj):
        return any(account_flags(a)[1] for a in self._visible_accounts(obj))


class ImplementationSerializer(ClientOwnedSerializer):
    client = ScopedPK(queryset=Client.objects.all())
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    responsible = ScopedPK(queryset=User.objects.all(), required=False, allow_null=True)
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)
    product_name = L10nField(source="product.name", read_only=True)
    responsible_name = serializers.CharField(source="responsible.full_name", read_only=True, default=None)
    overdue = serializers.BooleanField(read_only=True)
    consistency_fields = ("responsible",)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["checklist"] = [{**r, "label": loc_text(r.get("item", ""), r.get("item_ky", ""))}
                             for r in (data.get("checklist") or [])]
        return data

    class Meta:
        model = Implementation
        fields = ("id", "franchisee", "client", "client_name", "franchisee_name", "product", "product_name",
                  "stage", "started_at", "planned_launch", "launched_at", "responsible", "responsible_name",
                  "note", "overdue")
        read_only_fields = ("franchisee", "launched_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            attrs.pop("stage", None)  # этап меняется действием stage
        return attrs


class StageSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=Implementation.Stage.choices,
                                    error_messages={"invalid_choice": gettext_lazy("Неизвестный этап внедрения.")})


MANUAL_KINDS_FRANCHISEE = ("setup", "equipment", "other")


class ClientSaleSerializer(ClientOwnedSerializer):
    client = ScopedPK(queryset=Client.objects.all())
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    equipment = serializers.PrimaryKeyRelatedField(queryset=Equipment.objects.all(), required=False, allow_null=True)
    tariff = serializers.PrimaryKeyRelatedField(queryset=Tariff.objects.all(), required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    product_name = L10nField(source="product.name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True)
    sphere = L10nField(source="product.sphere", read_only=True)
    equipment_name = L10nField(source="equipment.name", read_only=True, default=None)
    tariff_name = L10nField(source="tariff.name", read_only=True, default=None)
    commission_rule_name = serializers.CharField(source="commission_rule.name", read_only=True, default=None)
    net_income = serializers.SerializerMethodField()

    class Meta:
        model = ClientSale
        fields = ("id", "franchisee", "client", "client_name", "franchisee_name", "product", "product_name",
                  "product_code", "sphere", "kind", "date", "amount", "equipment", "equipment_name", "quantity",
                  "tariff", "tariff_name", "list_amount", "discount_amount", "commission_rule",
                  "commission_rule_name", "commission_percent", "commission_fixed", "commission_base_amount",
                  "commission_amount", "franchisee_income", "income_overridden", "withheld_amount", "net_income",
                  "lead", "lead_source", "partner_tier", "adjustment_rule", "adjustment_amount",
                  "nbs_share_amount", "deal", "cancelled_at", "cancel_reason",
                  "paid_until", "months", "source", "external_payment_id", "comment")
        read_only_fields = ("franchisee", "commission_amount", "months", "source", "external_payment_id",
                            "list_amount", "discount_amount", "commission_rule", "commission_fixed",
                            "commission_base_amount", "income_overridden", "withheld_amount", "lead", "lead_source",
                            "partner_tier", "adjustment_rule", "adjustment_amount", "nbs_share_amount", "deal",
                            "cancelled_at", "cancel_reason")

    def get_net_income(self, obj):
        return money(obj.franchisee_income - obj.withheld_amount - obj.nbs_share_amount)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(gettext("Сумма должна быть больше нуля."))
        return value

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError(gettext("Количество должно быть больше нуля."))
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        if self.instance is not None:
            allowed = {"comment"}
            if user.role == ADMIN:
                allowed |= {"paid_until", "commission_percent", "franchisee_income"}
            if user.role == ACCOUNTANT:
                allowed = {"commission_percent", "franchisee_income"}
            return {k: v for k, v in attrs.items() if k in allowed}
        attrs.pop("commission_percent", None)
        attrs.pop("franchisee_income", None)
        kind = attrs.get("kind")
        if kind is None:
            raise serializers.ValidationError({"kind": [gettext("Укажите вид продажи.")]})
        if user.franchisee_id and kind not in MANUAL_KINDS_FRANCHISEE:
            raise serializers.ValidationError({"kind": [
                gettext("Франчайзи вносит вручную установку, оборудование и прочее: абонплата приходит из продукта.")]})
        equipment, product = attrs.get("equipment"), attrs["product"]
        if equipment is not None:
            if kind != "equipment":
                raise serializers.ValidationError({"equipment": [gettext("Оборудование указывается только для продажи оборудования.")]})
            if not equipment.is_active:
                raise serializers.ValidationError({"equipment": [gettext("Оборудование отключено.")]})
        if attrs.get("tariff") is not None and attrs["tariff"].product_id != product.id:
            raise serializers.ValidationError({"tariff": [gettext("Тариф относится к другой сфере.")]})
        if "amount" not in attrs:
            if equipment is None:
                raise serializers.ValidationError({"amount": [gettext("Укажите сумму.")]})
            attrs["amount"] = q2(equipment.price * attrs.get("quantity", 1))  # цена × количество по справочнику
        return super().validate(attrs)

    def create(self, validated_data):
        validated_data.pop("franchisee", None)
        sale, warning = services.create_client_sale(
            client=validated_data["client"], product=validated_data["product"], kind=validated_data["kind"],
            amount=validated_data["amount"], date=validated_data.get("date"),
            paid_until=validated_data.get("paid_until"), comment=validated_data.get("comment", ""),
            created_by=self.context["request"].user, equipment=validated_data.get("equipment"),
            quantity=validated_data.get("quantity", 1), tariff=validated_data.get("tariff"))
        sale._warning = warning
        sale._created = True
        return sale

    def update(self, instance, validated_data):
        percent = validated_data.pop("commission_percent", None)
        income = validated_data.pop("franchisee_income", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if income is not None:
            services.set_income(instance, income)
        elif percent is not None and Decimal(percent) != instance.commission_percent:
            services.set_commission_percent(instance, percent)
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if getattr(instance, "_created", False):
            data["warning"] = instance._warning
        return data



class AssignmentHistorySerializer(serializers.ModelSerializer):
    from_partner_name = serializers.CharField(source="from_partner.name", read_only=True, default=None)
    to_partner_name = serializers.CharField(source="to_partner.name", read_only=True, default=None)
    decided_by_name = serializers.CharField(source="decided_by.full_name", read_only=True, default=None)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    client = serializers.IntegerField(source="branch.client_id", read_only=True)
    client_name = serializers.CharField(source="branch.client.business_name", read_only=True)

    class Meta:
        model = BranchAssignmentHistory
        fields = ("id", "branch", "branch_name", "client", "client_name", "field", "from_partner",
                  "from_partner_name", "from_priority", "to_partner", "to_partner_name", "reason_code", "reason", "decided_by",
                  "decided_by_name", "effective_from", "created_at")
        read_only_fields = fields


class BranchSerializer(serializers.ModelSerializer):
    client = ScopedPK(queryset=Client.objects.all())
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    product_code = serializers.CharField(source="product.code", read_only=True, default=None)
    elqr_account = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=64)
    priority_expired = serializers.BooleanField(read_only=True)
    priority_hours_left = serializers.SerializerMethodField()
    assignment_history = AssignmentHistorySerializer(many=True, read_only=True)
    subscriptions = AccountBriefSerializer(many=True, read_only=True)
    implementer_partner_name = serializers.CharField(source="implementer_partner.name", read_only=True, default=None)

    class Meta:
        model = Branch
        fields = ("id", "client", "client_name", "name", "address", "city", "region", "product", "product_code",
                  "tariff", "launch_date", "elqr_account", "seller_partner", "implementer_partner",
                  "implementer_partner_name", "responsible_partner", "support_user", "is_main", "status",
                  "commission_enabled", "commission_enabled_from", "priority_partner", "priority_deadline", "priority_hours_left",
                  "priority_accepted_at", "priority_expired", "lead",
                  "lead_source", "assignment_history", "subscriptions", "created_at")
        read_only_fields = ("priority_partner", "priority_deadline", "priority_accepted_at", "lead_source", "status",
                            "is_main", "commission_enabled", "commission_enabled_from")
        validators = []

    def get_priority_hours_left(self, obj):
        """Сколько часов осталось у приоритетного партнёра на принятие второго филиала (None — не применимо)."""
        if not obj.priority_partner_id or obj.priority_accepted_at is not None or obj.implementer_partner_id                 or obj.priority_deadline is None:
            return None
        from django.utils import timezone
        return max(int((obj.priority_deadline - timezone.now()).total_seconds() // 3600), 0)

    def validate_elqr_account(self, value):
        value = (value or "").strip() or None
        if value and Branch.objects.filter(elqr_account=value).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError(gettext("Этот лицевой счёт Элкюар уже привязан к другому филиалу."))
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        product, tariff = attrs.get("product"), attrs.get("tariff")
        if tariff is not None and product is not None and tariff.product_id != product.id:
            raise serializers.ValidationError({"tariff": [gettext("Тариф относится к другой сфере.")]})
        if tariff is not None and product is None and self.instance is None:
            attrs["product"] = tariff.product
        if self.instance is not None:
            # закрепление меняется только действиями accept/reassign (история с датой вступления)
            for f in ("seller_partner", "implementer_partner", "responsible_partner", "client", "lead"):
                attrs.pop(f, None)
        if user.franchisee_id:
            for f in ("seller_partner", "implementer_partner", "responsible_partner", "support_user", "lead",
                      "elqr_account"):
                attrs.pop(f, None)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user = self.context["request"].user
        branch = Branch(**validated_data)
        if branch.lead_id:
            branch.lead_source = branch.lead.source
        branch_svc.setup_new_branch(branch, user=user)
        if not branch.lead_id and branch.is_main and branch.client.lead_id:
            branch.lead, branch.lead_source = branch.client.lead, branch.client.lead.source
        branch.save()
        return branch


class ClientContactSerializer(serializers.ModelSerializer):
    client = ScopedPK(queryset=Client.objects.all())

    class Meta:
        model = ClientContact
        fields = ("id", "client", "branch", "full_name", "position", "phone", "email", "is_primary")

    def validate(self, attrs):
        branch, client = attrs.get("branch"), attrs.get("client") or getattr(self.instance, "client", None)
        if branch is not None and client is not None and branch.client_id != client.id:
            raise serializers.ValidationError({"branch": [gettext("Филиал относится к другому клиенту.")]})
        if self.instance is not None:
            attrs.pop("client", None)
        return attrs


# ---- P7: установки, оборудование, шаблоны ----------------------------------------------------------------------
from . import installations as inst_svc  # noqa: E402
from .models import (ChecklistTemplate, ContractTemplate, ImplementerAccrual,  # noqa: E402
                     InstallationComment)


class InstallationSerializer(ClientOwnedSerializer):
    client = ScopedPK(queryset=Client.objects.all())
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    branch = ScopedPK(queryset=Branch.objects.all(), scope=None, required=False, allow_null=True)
    implementer = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(franchisee__isnull=True),
                                                     required=False, allow_null=True)
    responsible = ScopedPK(queryset=User.objects.all(), required=False, allow_null=True)
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)
    product_name = L10nField(source="product.name", read_only=True)
    implementer_name = serializers.CharField(source="implementer.full_name", read_only=True, default=None)
    overdue = serializers.BooleanField(read_only=True)
    technical_status = serializers.SerializerMethodField()
    checklist_progress = serializers.SerializerMethodField()
    implementer_accrual = serializers.SerializerMethodField()
    consistency_fields = ("responsible",)

    class Meta:
        model = Implementation
        fields = ("id", "franchisee", "franchisee_name", "client", "client_name", "product", "product_name", "deal",
                  "branch", "implementer", "implementer_name", "responsible", "status", "stage", "started_at",
                  "planned_launch", "deadline", "launched_at", "checklist", "checklist_progress", "training_result",
                  "client_rating", "act_file", "act_confirmed_at", "client_confirmed_by", "technical_channels",
                  "technical_status", "implementer_accrual", "note", "overdue")
        read_only_fields = ("franchisee", "deal", "status", "stage", "launched_at", "checklist", "training_result",
                            "client_rating", "act_file", "act_confirmed_at", "client_confirmed_by",
                            "technical_channels")

    def get_technical_status(self, obj):
        return inst_svc.technical_status(obj)

    def get_checklist_progress(self, obj):
        items = obj.checklist or []
        return {"done": sum(1 for i in items if i.get("done")), "total": len(items)}

    def get_implementer_accrual(self, obj):
        a = getattr(obj, "implementer_accrual", None)
        return {"id": a.id, "amount": str(a.amount), "status": a.status} if a else None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        client, branch = attrs.get("client"), attrs.get("branch")
        if branch is not None and client is not None and branch.client_id != client.id:
            raise serializers.ValidationError({"branch": [gettext("Филиал относится к другому клиенту.")]})
        user = self.context["request"].user
        if user.franchisee_id and "implementer" in attrs:
            raise serializers.ValidationError({"implementer": [gettext("Внедренца назначает OZOK.")]})
        return attrs

    def create(self, validated_data):
        inst = super().create(validated_data)
        inst.checklist = inst_svc.checklist_for(inst.product)
        inst.technical_channels = inst_svc.channels_for(inst.product, inst.started_at)
        if inst.deadline is None:
            from core.settings_registry import get_int
            from datetime import timedelta
            inst.deadline = inst.started_at + timedelta(days=get_int("installations.default_deadline_days"))
        inst.save()
        return inst


class InstallationCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.full_name", read_only=True, default=None)

    class Meta:
        model = InstallationComment
        fields = ("id", "installation", "author", "author_name", "text", "created_at")
        read_only_fields = fields


class ImplementerAccrualSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    client_name = serializers.CharField(source="installation.client.business_name", read_only=True)
    product_code = serializers.CharField(source="installation.product.code", read_only=True)

    class Meta:
        model = ImplementerAccrual
        fields = ("id", "installation", "client_name", "product_code", "user", "user_name", "amount", "sold_amount",
                  "split_rule", "status", "verified_by", "verified_at", "paid_by", "paid_at", "payment_ref",
                  "created_at")
        read_only_fields = fields


class ChecklistTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistTemplate
        fields = ("id", "product", "name", "items", "is_active")

    def validate_items(self, value):
        """Пункты: строки или {ru, ky}; хранятся парами {ru, ky} (ky необязателен)."""
        if not isinstance(value, list) or not all(isinstance(i, (str, dict)) for i in value):
            raise serializers.ValidationError(gettext("Пункты — список строк."))
        return reference_ky.normalize_items(value)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["items"] = reference_ky.normalize_items(instance.items)
        data["labels"] = [loc_text(i["ru"], i["ky"]) for i in data["items"]]
        return data


class ContractTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContractTemplate
        fields = ("id", "kind", "name", "file", "is_active")


class EquipmentSaleSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    equipment_name = L10nField(source="equipment.name", read_only=True, default=None)
    sold_by_name = serializers.CharField(source="sold_by.name", read_only=True, default=None)
    installed_by_name = serializers.CharField(source="installed_by.full_name", read_only=True, default=None)

    class Meta:
        model = ClientSale
        fields = ("id", "franchisee", "client", "client_name", "product", "equipment", "equipment_name", "quantity",
                  "amount", "date", "serial_numbers", "sold_by", "sold_by_name", "installed_by", "installed_by_name",
                  "delivery_status", "warranty_until", "deal", "cancelled_at")
        read_only_fields = ("franchisee", "client", "product", "equipment", "quantity", "amount", "date", "sold_by",
                            "deal", "warranty_until", "cancelled_at")

    def validate_serial_numbers(self, value):
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            raise serializers.ValidationError(gettext("Серийные номера — список строк."))
        qty = int(self.instance.quantity) if self.instance else 0
        if qty and len(value) > qty:
            raise serializers.ValidationError(gettext("Серийных номеров больше, чем единиц оборудования."))
        return value

    def validate_installed_by(self, value):
        if value is not None and value.franchisee_id not in (None, self.instance.franchisee_id):
            raise serializers.ValidationError(gettext("Установщик — сотрудник OZOK или этого партнёра."))
        return value

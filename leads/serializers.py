from django.utils.translation import gettext, gettext_lazy
from rest_framework import serializers

from core.serializers import ScopedPK
from network.models import Franchisee, Product

from .models import Lead, LeadActivity
from .services import create_lead

PARTNER_EDITABLE = {"comment", "company_name", "contact_name", "name", "phone", "city", "region", "next_contact_at",
                    "interest", "product"}


def validate_product_value(value):
    """Код сферы, package:<id>, help или пусто; число — id продукта (преобразуется в код)."""
    value = str(value) if value is not None else ""
    if value in ("", "help"):
        return value
    if value.startswith("package:") and value[8:].isdigit():
        return value
    if value.isdigit():
        p = Product.objects.filter(pk=int(value)).first()
        if p:
            return p.code
    if not Product.objects.filter(code=value).exists():
        raise serializers.ValidationError(gettext("Неизвестная сфера: укажите код сферы, package:<id>, help или пусто."))
    return value


class LeadSerializer(serializers.ModelSerializer):
    franchisee = ScopedPK(queryset=Franchisee.objects.all(), scope="id", required=False, allow_null=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)
    product = serializers.CharField(required=False, allow_blank=True)
    overdue = serializers.BooleanField(read_only=True)
    client_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Lead
        fields = ("id", "source", "interest", "product", "name", "company_name", "contact_name", "phone", "city",
                  "region", "comment", "status", "franchisee", "franchisee_name", "implementer", "next_contact_at",
                  "owner_confirmed", "owner_confirmed_by", "owner_confirmed_at", "assigned_at", "taken_at",
                  "closed_at", "lost_reason", "client_id", "overdue", "created_at")
        read_only_fields = ("status", "assigned_at", "taken_at", "closed_at", "lost_reason", "owner_confirmed",
                            "owner_confirmed_by", "owner_confirmed_at")

    def validate_product(self, value):
        return validate_product_value(value)

    def validate(self, attrs):
        user = self.context["request"].user
        if self.instance is None:
            if user.franchisee_id:
                attrs["franchisee"] = user.franchisee
                attrs.pop("implementer", None)
        else:
            if user.franchisee_id:
                locked = set(self.initial_data) & {"source", "franchisee", "implementer"}
                if locked:
                    raise serializers.ValidationError(
                        gettext("Источник, партнёра и ответственного за внедрение меняет только OZOK: "
                        "отправьте запрос leads/{id}/change-request/."))
                extra = set(self.initial_data) - PARTNER_EDITABLE
                if extra:
                    raise serializers.ValidationError(gettext("Это поле партнёр изменить не может."))
            attrs.pop("franchisee", None)  # переназначение — только действием assign
        return attrs

    def create(self, validated_data):
        franchisee = validated_data.pop("franchisee", None)
        user = self.context["request"].user
        return create_lead(franchisee=franchisee, user=user, by_partner=bool(user.franchisee_id), **validated_data)


class PublicLeadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = ("name", "phone", "city", "interest", "product", "comment")
        extra_kwargs = {"city": {"required": False}, "product": {"required": False},
                        "comment": {"required": False}, "interest": {"required": True}}

    def validate_product(self, value):
        return validate_product_value(value)


class AssignSerializer(serializers.Serializer):
    franchisee = serializers.PrimaryKeyRelatedField(queryset=Franchisee.objects.all(),
                                                    error_messages={"does_not_exist": gettext_lazy("Франчайзи не найден."),
                                                                    "required": gettext_lazy("Укажите франчайзи.")})
    reason = serializers.CharField(required=False, allow_blank=True)


class StatusSerializer(serializers.Serializer):
    status = serializers.CharField()


class WinSerializer(serializers.Serializer):
    business_name = serializers.CharField(required=False, allow_blank=True)
    business_type = serializers.CharField(required=False, allow_blank=True)


class LoseSerializer(serializers.Serializer):
    lost_reason = serializers.CharField(error_messages={"required": gettext_lazy("Укажите причину отказа."),
                                                        "blank": gettext_lazy("Укажите причину отказа.")})


class ChangeRequestInSerializer(serializers.Serializer):
    field = serializers.CharField()
    value = serializers.JSONField()
    reason = serializers.CharField(error_messages={"required": gettext_lazy("Укажите причину."), "blank": gettext_lazy("Укажите причину.")})


class LeadActivitySerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True, default=None)

    class Meta:
        model = LeadActivity
        fields = ("id", "lead", "user", "user_name", "type", "text", "created_at")
        read_only_fields = ("lead", "user", "created_at")

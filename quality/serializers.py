from django.utils.translation import gettext, gettext_lazy
from rest_framework import serializers

from accounts.models import User
from clients.models import Client
from core.l10n import loc_text
from core.serializers import ClientOwnedSerializer, OwnedSerializer, ScopedPK

from .models import Audit, Ticket
from .services import audit_score


class TicketSerializer(ClientOwnedSerializer):
    client = ScopedPK(queryset=Client.objects.all())
    assignee = ScopedPK(queryset=User.objects.all(), required=False, allow_null=True)
    client_name = serializers.CharField(source="client.business_name", read_only=True)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    assignee_name = serializers.CharField(source="assignee.full_name", read_only=True, default=None)
    sla_breached = serializers.BooleanField(read_only=True)
    sla_hours = serializers.IntegerField(read_only=True)
    consistency_fields = ("assignee",)

    class Meta:
        model = Ticket
        fields = ("id", "franchisee", "client", "client_name", "franchisee_name", "subject", "description",
                  "priority", "status", "opened_at", "first_response_at", "resolved_at", "rating", "assignee",
                  "assignee_name", "sla_breached", "sla_hours")
        read_only_fields = ("franchisee", "status", "first_response_at", "resolved_at", "rating")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        user = self.context["request"].user
        if user.role != "super_admin":
            attrs.pop("opened_at", None)  # время открытия ставит сервер: иначе можно «подогнать» SLA
        elif self.instance is not None:
            attrs.pop("opened_at", None)
        if self.instance is not None and user.franchisee_id:
            attrs.pop("priority", None)  # приоритет (а с ним норматив SLA) франчайзи после создания не меняет
        return attrs


class StatusSerializer(serializers.Serializer):
    status = serializers.CharField(error_messages={"required": gettext_lazy("Укажите статус.")})


class RatingSerializer(serializers.Serializer):
    rating = serializers.IntegerField(error_messages={"required": gettext_lazy("Укажите оценку."),
                                                      "invalid": gettext_lazy("Оценка — целое число от 1 до 5.")})


class AuditSerializer(OwnedSerializer):
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    inspector_name = serializers.CharField(source="inspector.full_name", read_only=True, default=None)

    class Meta:
        model = Audit
        fields = ("id", "franchisee", "franchisee_name", "date", "inspector", "inspector_name", "items", "score",
                  "summary")
        read_only_fields = ("inspector", "score")

    def validate_items(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError(gettext("Чек-лист должен быть непустым списком критериев."))
        items = []
        for i in value:
            if not isinstance(i, dict) or not str(i.get("criterion", "")).strip() or not isinstance(i.get("ok"), bool):
                raise serializers.ValidationError(gettext('Каждый пункт: {"criterion": "...", "ok": true|false, "comment": ""}.'))
            items.append({"criterion": str(i["criterion"]).strip(), "criterion_ky": str(i.get("criterion_ky", "")).strip(),
                          "ok": i["ok"], "comment": str(i.get("comment", ""))})
        return items

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["items"] = [{**i, "criterion_ky": i.get("criterion_ky", ""),
                          "label": loc_text(i.get("criterion", ""), i.get("criterion_ky", ""))}
                         for i in (data.get("items") or [])]
        return data

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if "items" in attrs:
            attrs["score"] = audit_score(attrs["items"])
        elif self.instance is None:
            raise serializers.ValidationError({"items": [gettext("Заполните чек-лист.")]})
        return attrs

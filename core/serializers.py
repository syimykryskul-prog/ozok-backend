"""Общие классы сериализаторов: проверка видимости FK, franchisee на сервере."""
from django.utils.translation import gettext, gettext_lazy
from rest_framework import serializers


class ScopedPK(serializers.PrimaryKeyRelatedField):
    """FK, который пользователь франчайзи видит только в рамках своего франчайзи.

    scope — lookup по franchisee (например "franchisee_id" или "id" для самого франчайзи).
    """

    default_error_messages = {
        "required": gettext_lazy("Обязательное поле."),
        "does_not_exist": gettext_lazy("Объект не найден."),
        "incorrect_type": gettext_lazy("Неверный тип значения."),
        "null": gettext_lazy("Это поле не может быть пустым."),
    }

    def __init__(self, queryset=None, scope="franchisee_id", **kwargs):
        self.scope = scope
        super().__init__(queryset=queryset, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.context["request"].user
        if user.franchisee_id and self.scope:
            return qs.filter(**{self.scope: user.franchisee_id})
        return qs


def check_same_franchisee(franchisee_id, **objs):
    """Все связанные объекты должны принадлежать одному франчайзи."""
    errors = {}
    for name, obj in objs.items():
        if obj is not None and obj.franchisee_id != franchisee_id:
            errors[name] = [gettext("Объект принадлежит другому франчайзи.")]
    if errors:
        raise serializers.ValidationError(errors)


class OwnedSerializer(serializers.ModelSerializer):
    """Объект с прямым полем franchisee.

    Франчайзи: проставляется сервером. Центр: указывает явно при создании;
    после создания не меняется. consistency_fields — FK, которые обязаны
    принадлежать тому же франчайзи.
    """

    consistency_fields = ()

    def get_fields(self):
        fields = super().get_fields()
        from network.models import Franchisee
        fields["franchisee"] = ScopedPK(queryset=Franchisee.objects.all(), scope="id", required=False)
        return fields

    def resolve_franchisee(self, attrs):
        user = self.context["request"].user
        if self.instance is None:
            if user.franchisee_id:
                attrs["franchisee"] = user.franchisee
            elif not attrs.get("franchisee"):
                raise serializers.ValidationError({"franchisee": [gettext("Укажите франчайзи.")]})
            return attrs["franchisee"]
        attrs.pop("franchisee", None)
        return self.instance.franchisee

    def validate(self, attrs):
        attrs = super().validate(attrs)
        fr = self.resolve_franchisee(attrs)
        check_same_franchisee(fr.id, **{n: attrs.get(n) for n in self.consistency_fields})
        return attrs


class ClientOwnedSerializer(serializers.ModelSerializer):
    """Объект, франчайзи которого берётся из клиента (поле client)."""

    consistency_fields = ()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None:
            client = attrs.get("client")
            if client is None:
                raise serializers.ValidationError({"client": [gettext("Укажите клиента.")]})
            attrs["franchisee"] = client.franchisee
            fr = client.franchisee
        else:
            attrs.pop("client", None)
            fr = self.instance.franchisee
        check_same_franchisee(fr.id, **{n: attrs.get(n) for n in self.consistency_fields})
        return attrs



from .models import AuditLog, ChangeRequest  # noqa: E402


class ChangeRequestSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.CharField(source="requested_by.full_name", read_only=True, default=None)
    decided_by_name = serializers.CharField(source="decided_by.full_name", read_only=True, default=None)

    class Meta:
        model = ChangeRequest
        fields = ("id", "requested_by", "requested_by_name", "entity", "entity_id", "field", "old_value",
                  "new_value", "reason", "status", "decided_by", "decided_by_name", "decided_at",
                  "decision_comment", "franchisee", "created_at")
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ("id", "user", "user_email", "action", "entity", "entity_id", "changes", "reason", "ip",
                  "user_agent", "created_at")

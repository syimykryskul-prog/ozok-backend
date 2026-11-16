from django.utils.translation import gettext_lazy
from rest_framework import serializers

from core.l10n import L10nField

from clients.models import Client
from network.models import Franchisee

from .models import IntegrationEvent


class IntegrationEventSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True, default=None)
    product_name = L10nField(source="product.name", read_only=True, default=None)
    client_name = serializers.CharField(source="client.business_name", read_only=True, default=None)
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True, default=None)

    class Meta:
        model = IntegrationEvent
        fields = ("id", "provider", "product", "product_code", "product_name", "event_type", "external_id", "payload",
                  "status", "error", "client", "client_name", "franchisee", "franchisee_name", "received_at",
                  "processed_at")
        read_only_fields = fields


class ResolveSerializer(serializers.Serializer):
    franchisee = serializers.PrimaryKeyRelatedField(queryset=Franchisee.objects.all(),
                                                    error_messages={"does_not_exist": gettext_lazy("Франчайзи не найден."),
                                                                    "required": gettext_lazy("Укажите франчайзи.")})
    client = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all(), required=False, allow_null=True,
                                                error_messages={"does_not_exist": gettext_lazy("Клиент не найден.")})

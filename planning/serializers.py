from django.utils.translation import gettext, gettext_lazy
from rest_framework import serializers

from core.models import money, parse_month
from core.serializers import OwnedSerializer

from .models import SalesPlan
from .services import month_facts, plan_percent, _r2


class MonthField(serializers.Field):
    """Вход: 'YYYY-MM' или дата; хранится 1-е число; выход: 'YYYY-MM-01'."""

    default_error_messages = {"invalid": gettext_lazy("Неверный формат месяца, нужен YYYY-MM.")}

    def to_internal_value(self, data):
        try:
            return parse_month(data)
        except Exception:
            self.fail("invalid")

    def to_representation(self, value):
        return value.isoformat()


class SalesPlanSerializer(OwnedSerializer):
    month = MonthField()
    franchisee_name = serializers.CharField(source="franchisee.name", read_only=True)
    fact_clients = serializers.SerializerMethodField()
    fact_revenue = serializers.SerializerMethodField()
    plan_percent = serializers.SerializerMethodField()

    class Meta:
        model = SalesPlan
        fields = ("id", "franchisee", "franchisee_name", "month", "target_clients", "target_revenue",
                  "fact_clients", "fact_revenue", "plan_percent")
        validators = []

    def _fact(self, obj):
        cache = self.context.setdefault("_cache", {})
        if obj.month not in cache:
            cache[obj.month] = month_facts(obj.month)
        return cache[obj.month].get(obj.franchisee_id, (0, 0))

    def get_fact_clients(self, obj):
        return self._fact(obj)[0]

    def get_fact_revenue(self, obj):
        return money(self._fact(obj)[1])

    def get_plan_percent(self, obj):
        nc, rev = self._fact(obj)
        return _r2(plan_percent(obj.target_clients, obj.target_revenue, nc, rev))

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            attrs.pop("month", None)
        else:
            if SalesPlan.objects.filter(franchisee=attrs["franchisee"], month=attrs["month"]).exists():
                raise serializers.ValidationError(gettext("План на этот месяц у франчайзи уже задан."))
        return attrs

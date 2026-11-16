from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters import rest_framework as df

from core.filters import build_filterset
from core.models import parse_month
from core.permissions import RolePermission
from core.viewsets import ScopedViewSet

from .models import SalesPlan
from .serializers import SalesPlanSerializer
from .services import rating_table


def _month(qs, name, value):
    return qs.filter(month=parse_month(value)) if value else qs


class SalesPlanViewSet(ScopedViewSet):
    resource = "sales_plans"
    queryset = SalesPlan.objects.select_related("franchisee")
    serializer_class = SalesPlanSerializer
    filterset_class = build_filterset(SalesPlan, month=df.CharFilter(method=_month))
    ordering_fields = ("id", "month", "target_revenue")


class RatingReportView(APIView):
    """GET reports/rating/?month=YYYY-MM — таблица рейтинга сети (только центр)."""

    resource = "reports"
    permission_classes = [RolePermission]

    def get(self, request):
        return Response(rating_table(parse_month(request.query_params.get("month"))))

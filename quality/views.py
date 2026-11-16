from rest_framework.decorators import action
from rest_framework.response import Response

from core.filters import build_filterset, flag
from core.viewsets import ScopedViewSet

from . import services
from .models import Audit, Ticket
from .serializers import AuditSerializer, RatingSerializer, StatusSerializer, TicketSerializer


def _sla(qs, name, value):
    ids = [t.id for t in qs if t.sla_breached]
    return qs.filter(id__in=ids) if value else qs.exclude(id__in=ids)


class TicketViewSet(ScopedViewSet):
    resource = "tickets"
    queryset = Ticket.objects.select_related("franchisee", "client", "assignee")
    serializer_class = TicketSerializer
    filterset_class = build_filterset(Ticket, exact=("status", "priority"), ids=("client",), sla_breached=flag(_sla))
    search_fields = ("subject", "client__business_name")
    ordering_fields = ("id", "opened_at", "priority", "status")

    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        ser = StatusSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ticket = services.change_status(self.get_object(), ser.validated_data["status"])
        return Response(self.get_serializer(ticket).data)

    @action(detail=True, methods=["post"])
    def rate(self, request, pk=None):
        ser = RatingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ticket = services.rate_ticket(self.get_object(), ser.validated_data["rating"])
        return Response(self.get_serializer(ticket).data)


class AuditViewSet(ScopedViewSet):
    resource = "audits"
    queryset = Audit.objects.select_related("franchisee", "inspector")
    serializer_class = AuditSerializer
    filterset_class = build_filterset(Audit, date_field="date")
    search_fields = ("summary",)
    ordering_fields = ("id", "date", "score")

    def perform_create(self, serializer):
        serializer.save(inspector=self.request.user)

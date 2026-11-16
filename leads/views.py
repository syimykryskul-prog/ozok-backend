from django.utils.translation import gettext
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.filters import build_filterset, flag
from core.viewsets import ScopedViewSet

from . import services
from .models import Lead, LeadActivity, overdue_hours
from .serializers import (AssignSerializer, ChangeRequestInSerializer, LeadActivitySerializer, LeadSerializer,
                          LoseSerializer, PublicLeadSerializer, StatusSerializer, WinSerializer)


class PublicLeadThrottle(AnonRateThrottle):
    scope = "public_leads"  # 10/min с IP (settings.REST_FRAMEWORK.DEFAULT_THROTTLE_RATES)


def _overdue(qs, name, value):
    now = timezone.now()
    cond = (Q(status="new", franchisee__isnull=False, assigned_at__lt=now - timedelta(hours=overdue_hours()))
            | Q(status__in=Lead.OPEN_STATUSES, next_contact_at__lt=now))
    return qs.filter(cond) if value else qs.exclude(cond)


def _unassigned(qs, name, value):
    cond = Q(franchisee__isnull=True, status="new")
    return qs.filter(cond) if value else qs.exclude(cond)


class LeadViewSet(ScopedViewSet):
    resource = "leads"
    queryset = Lead.objects.select_related("franchisee")
    serializer_class = LeadSerializer
    filterset_class = build_filterset(Lead, exact=("status", "interest", "product", "source", "city", "region"),
                                      bools=("owner_confirmed",), overdue=flag(_overdue),
                                      unassigned=flag(_unassigned))
    search_fields = ("name", "phone", "city", "company_name")
    ordering_fields = ("id", "created_at", "status", "assigned_at", "name", "city", "next_contact_at")

    def _reply(self, lead):
        return Response(self.get_serializer(lead).data)

    def _own(self):
        """M10: запрос партнёра на подключение (interest=franchise) партнёр не ведёт сам — решает OZOK."""
        lead = self.get_object()
        if self.request.user.franchisee_id and lead.interest == Lead.Interest.FRANCHISE:
            raise PermissionDenied(gettext("Запрос на подключение рассматривает OZOK."))
        return lead

    def update(self, request, *args, **kwargs):
        if request.user.franchisee_id and self.get_object().interest == Lead.Interest.FRANCHISE:
            raise PermissionDenied(gettext("Запрос на подключение рассматривает OZOK."))
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        ser = AssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._reply(services.assign_lead(self.get_object(), ser.validated_data["franchisee"],
                                                reason=ser.validated_data.get("reason", ""), user=request.user))

    @action(detail=True, methods=["post"])
    def take(self, request, pk=None):
        return self._reply(services.take_lead(self._own(), user=request.user))

    @action(detail=True, methods=["post"])
    def demo(self, request, pk=None):
        return self._reply(services.demo_lead(self._own(), user=request.user))

    @action(detail=True, methods=["post"], url_path="status")
    def status(self, request, pk=None):
        ser = StatusSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._reply(services.set_status(self._own(), ser.validated_data["status"], user=request.user))

    @action(detail=True, methods=["post"])
    def win(self, request, pk=None):
        ser = WinSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._reply(services.win_lead(self._own(), user=request.user, **ser.validated_data))

    @action(detail=True, methods=["post"])
    def lose(self, request, pk=None):
        ser = LoseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._reply(services.lose_lead(self._own(), ser.validated_data["lost_reason"], user=request.user))

    @action(detail=True, methods=["post"], url_path="confirm-owner")
    def confirm_owner(self, request, pk=None):
        return self._reply(services.confirm_owner(self.get_object(), request.user))

    @action(detail=True, methods=["post"], url_path="change-request")
    def change_request(self, request, pk=None):
        from core.serializers import ChangeRequestSerializer
        ser = ChangeRequestInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cr = services.request_change(self.get_object(), request.user, **ser.validated_data)
        return Response(ChangeRequestSerializer(cr).data, status=201)

    @action(detail=True, methods=["get", "post"])
    def activities(self, request, pk=None):
        lead = self.get_object()
        if request.method == "POST":
            ser = LeadActivitySerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            act = services.log(lead, request.user, ser.validated_data.get("type", "note"),
                               ser.validated_data.get("text", ""))
            return Response(LeadActivitySerializer(act).data, status=201)
        page = self.paginate_queryset(lead.activities.select_related("user"))
        return self.get_paginated_response(LeadActivitySerializer(page, many=True).data)


class PublicLeadView(APIView):
    """POST leads/public/ — заявка с сайта без авторизации (10/мин с IP)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicLeadThrottle]

    def post(self, request):
        ser = PublicLeadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        lead = services.create_lead(source=Lead.Source.SITE, **ser.validated_data)
        return Response({"id": lead.id}, status=status.HTTP_201_CREATED)

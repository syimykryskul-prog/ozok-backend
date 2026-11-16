from django.utils.translation import gettext
from rest_framework import permissions
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import BusinessError
from core.filters import build_filterset, product_filter
from core.viewsets import ScopedReadOnlyViewSet
from django_filters import rest_framework as df
from network.models import Product

from . import services
from .models import IntegrationEvent
from .serializers import IntegrationEventSerializer, ResolveSerializer

HEADER = "X-OZOK-Key"


class ProductKeyAuthentication(BaseAuthentication):
    """Ключ продукта в заголовке X-OZOK-Key. Неверный ключ или выключенная интеграция — 401."""

    def authenticate(self, request):
        key = request.headers.get(HEADER)
        if not key:
            return None
        product = Product.objects.filter(api_key=key, integration_enabled=True, is_active=True).first()
        if product is None:
            raise AuthenticationFailed(gettext("Неверный ключ, интеграция отключена или сфера неактивна."))
        return (None, product)

    def authenticate_header(self, request):
        return HEADER


class HasProductKey(permissions.BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, Product)


class _ProductAPIView(APIView):
    authentication_classes = [ProductKeyAuthentication]
    permission_classes = [HasProductKey]


class IntegrationEventsView(_ProductAPIView):
    """POST integrations/events/ — одно событие или массив до 100."""

    def post(self, request):
        data = request.data
        events = data if isinstance(data, list) else [data]
        if not events:
            raise BusinessError(gettext("Пустой список событий."))
        if len(events) > services.MAX_BATCH:
            raise BusinessError(gettext('В одном запросе можно передать не более {MAX_BATCH} событий.').format(MAX_BATCH=services.MAX_BATCH))
        return Response({"results": services.process_events(request.auth, events)})


class ElqrWebhookView(APIView):
    """POST integrations/elqr/webhook/ — подпись HMAC-SHA256 тела в заголовке X-Elqr-Signature (секрет в env)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        import json

        from django.conf import settings

        from billing.payments import verify_signature
        secret = getattr(settings, "ELQR_WEBHOOK_SECRET", "")
        if not secret:
            return Response({"detail": gettext("Вебхук Элкюар не настроен (ELQR_WEBHOOK_SECRET).")}, status=503)
        if not verify_signature(secret, request.body, request.headers.get("X-Elqr-Signature", "")):
            return Response({"detail": gettext("Неверная подпись.")}, status=401)
        try:
            data = json.loads(request.body.decode() or "{}")
        except ValueError:
            raise BusinessError(gettext("Тело запроса должно быть JSON."))
        event = services.process_elqr_event(data)
        return Response({"event_id": event.external_id or None, "status": event.status,
                         "error": event.error or None})


class IntegrationPingView(_ProductAPIView):
    def get(self, request):
        return Response({"product": request.auth.code, "ok": True})


class IntegrationEventViewSet(ScopedReadOnlyViewSet):
    resource = "integration_events"
    queryset = IntegrationEvent.objects.select_related("product", "client", "franchisee")
    serializer_class = IntegrationEventSerializer
    filterset_class = build_filterset(
        IntegrationEvent, exact=("status", "event_type"), product=product_filter(),
        date_from=df.DateFilter(field_name="received_at__date", lookup_expr="gte"),
        date_to=df.DateFilter(field_name="received_at__date", lookup_expr="lte"))
    search_fields = ("external_id", "error")
    ordering_fields = ("id", "received_at", "status")

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        return Response(self.get_serializer(services.retry_event(self.get_object())).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        ser = ResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        event = services.resolve_event(self.get_object(), ser.validated_data["franchisee"],
                                       ser.validated_data.get("client"))
        return Response(self.get_serializer(event).data)


class IntegrationTestEventView(APIView):
    """POST integrations/test-event/ {product} — hq_admin: симулирует payment.succeeded демо-клиента."""

    def post(self, request):
        from core.permissions import ADMIN
        from integrations.simulator import simulate
        if not (request.user and request.user.is_authenticated and request.user.role == ADMIN):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied(gettext("У вас нет прав для этого действия."))
        value = str(request.data.get("product", ""))
        product = (Product.objects.filter(pk=int(value)) if value.isdigit()
                   else Product.objects.filter(code=value)).first()
        if product is None:
            raise BusinessError(gettext("Укажите существующую сферу в поле product."))
        return Response({"results": simulate(product, 1)})

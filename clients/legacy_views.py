"""Существующие клиенты OZOK: ввод по одному и импорт по сфере (super_admin, finance и кому выдано право)."""
from django.utils.translation import gettext
from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import BusinessError
from core.permissions import RolePermission

from . import legacy
from .serializers import ClientSerializer


class _Base(APIView):
    resource = "legacy_clients"
    permission_classes = [RolePermission]

    def _text(self, request):
        f = request.FILES.get("file")
        if f is not None:
            return f.read().decode("utf-8-sig", errors="replace")
        if request.data.get("csv"):
            return str(request.data["csv"])
        raise BusinessError(gettext("Передайте CSV файлом (file) или текстом (csv)."))

    def _product(self, request):
        value = request.data.get("product") or request.query_params.get("product")
        if not value:
            raise BusinessError(gettext("Укажите сферу (product)."))
        return value


class LegacyClientCreateView(_Base):
    """POST clients/legacy/ — один клиент OZOK + список сфер."""

    action = "create"

    def post(self, request):
        client, subs = legacy.create_legacy_client(request.data, request.user)
        client = type(client).objects.prefetch_related("accounts__product", "sales__product",
                                                       "implementations__product", "branches", "contacts") \
            .get(pk=client.pk)
        return Response(ClientSerializer(client, context={"request": request}).data, status=201)


class LegacyImportTemplateView(_Base):
    """GET clients/legacy-import/template/?product= — CSV-шаблон сферы."""

    action = "template"

    def get(self, request):
        product = legacy._resolve_product(self._product(request))
        resp = HttpResponse(legacy.template_csv(product), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="legacy-clients-{product.code}.csv"'
        return resp


class LegacyImportPreviewView(_Base):
    action = "import_preview"

    def post(self, request):
        return Response(legacy.preview(self._text(request), self._product(request)))


class LegacyImportConfirmView(_Base):
    """POST clients/legacy-import/confirm/ {product, file|csv, duplicates: merge|skip, actions: {строка: merge|skip}}."""

    action = "import_confirm"

    def post(self, request):
        actions = request.data.get("actions")
        if isinstance(actions, str):
            import json
            try:
                actions = json.loads(actions)
            except ValueError:
                raise BusinessError(gettext("actions: ожидается JSON-объект {строка: merge|skip}."))
        return Response(legacy.confirm(self._text(request), self._product(request), request.user,
                                       duplicates=request.data.get("duplicates") or "skip", actions=actions))

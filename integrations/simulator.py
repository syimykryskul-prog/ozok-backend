"""Симулятор продукта: генерирует события для проверки интеграции без реального продукта."""
from django.utils.translation import gettext
import random
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from clients.models import ClientProductAccount
from core.exceptions import BusinessError
from core.models import add_months, today
from network.models import Franchisee

from .services import process_events


def simulate(product, count=1, rng=None):
    """count событий payment.succeeded по существующим аккаунтам продукта.

    Если аккаунтов нет — сначала создаёт демо-клиента через client.upsert (нужен франчайзи с partner_code).
    """
    rng = rng or random
    accounts = list(ClientProductAccount.objects.filter(product=product, status="active"))
    events = []
    if not accounts:
        fr = Franchisee.objects.filter(status="franchisee").exclude(partner_code=None).first()
        if fr is None:
            raise BusinessError(gettext("Нет франчайзи с кодом партнёра для тестового события."))
        ext = f"demo-{uuid.uuid4().hex[:8]}"
        events.append({"event_id": f"sim-{uuid.uuid4().hex}", "type": "client.upsert", "external_client_id": ext,
                       "partner_code": fr.partner_code, "business_name": gettext("Демо-клиент (тест интеграции)"),
                       "business_type": "shop", "city": fr.city})
        accounts_ext = [(ext, Decimal("2500"), None)]
    else:
        accounts_ext = [(a.external_id, a.monthly_fee or Decimal("2500"), a.paid_until) for a in accounts]
    for _ in range(count):
        ext, fee, paid_until = rng.choice(accounts_ext)
        months = rng.choice([1, 1, 3, 6])
        base = max(paid_until or today(), today())
        new_until = add_months(base, months)
        events.append({"event_id": f"sim-{uuid.uuid4().hex}", "type": "payment.succeeded",
                       "external_payment_id": f"sim-pay-{uuid.uuid4().hex[:12]}", "external_client_id": ext,
                       "amount": str(fee * months), "months": months, "paid_until": new_until.isoformat(),
                       "paid_at": timezone.now().isoformat()})
    return process_events(product, events)

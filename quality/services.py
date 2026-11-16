"""Бизнес-правила качества: SLA обращений, оценка, проверки."""
from django.utils.translation import gettext, gettext_lazy
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from core.exceptions import BusinessError

from .models import Ticket

DEFAULT_CHECKLIST = [
    gettext_lazy("Вывеска и материалы по брендбуку"),
    gettext_lazy("Ответ на заявку в течение 24 часов"),
    gettext_lazy("Демо по стандарту OZOK"),
    gettext_lazy("Сотрудники прошли обучение"),
    gettext_lazy("Клиенты получают сопровождение после запуска"),
    gettext_lazy("Отчётность сдаётся вовремя"),
]

TRANSITIONS = {
    "open": {"in_progress", "resolved", "closed"},
    "in_progress": {"open", "resolved", "closed"},
    "resolved": {"in_progress", "closed"},
    "closed": set(),
}


def create_ticket(*, now=None, **fields):
    fields.setdefault("opened_at", now or timezone.now())
    return Ticket.objects.create(**fields)


def change_status(ticket, status, now=None):
    if status not in Ticket.Status.values:
        raise BusinessError(gettext("Неизвестный статус."))
    if status not in TRANSITIONS[ticket.status]:
        raise BusinessError(gettext("Недопустимый переход статуса обращения."))
    now = now or timezone.now()
    if status != Ticket.Status.OPEN and ticket.first_response_at is None:
        ticket.first_response_at = now
    if status == Ticket.Status.RESOLVED:
        ticket.resolved_at = now
    ticket.status = status
    ticket.save()
    return ticket


def rate_ticket(ticket, rating):
    if ticket.status not in (Ticket.Status.RESOLVED, Ticket.Status.CLOSED):
        raise BusinessError(gettext("Оценить можно только решённое или закрытое обращение."))
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        raise BusinessError(gettext("Оценка должна быть целым числом от 1 до 5."))
    ticket.rating = rating
    ticket.save()
    return ticket


def audit_score(items):
    """round(100 × ok / всего), half-up."""
    if not items:
        return 0
    ok = sum(1 for i in items if i.get("ok"))
    return int((Decimal(100 * ok) / len(items)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

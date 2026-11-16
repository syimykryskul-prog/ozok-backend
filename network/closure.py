"""Уход франчайзи (P5, §7.3, H3): закрытие, блокировка доступа, передача клиентов/филиалов/обращений/лидов."""
from django.utils.translation import gettext
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from clients import branches as branch_svc
from clients.models import Branch, BranchAssignmentHistory, Client
from core import audit
from core.exceptions import BusinessError
from core.models import today

from . import partners
from .models import Contract, Franchisee

H = BranchAssignmentHistory
OPEN_LEAD = ("new", "in_progress", "demo", "proposal", "contract")


def revoke_tokens(user):
    """Отозвать refresh-токены пользователя (blacklist); access живёт ≤ 30 мин и сразу отклоняется is_active=false."""
    try:
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    except Exception:  # noqa: BLE001 — приложение blacklist не подключено
        return 0
    n = 0
    for tok in OutstandingToken.objects.filter(user=user):
        _, created = BlacklistedToken.objects.get_or_create(token=tok)
        n += int(created)
    return n


@transaction.atomic
def close_partner(partner, *, user, closed_at=None, reason, transfer_to_partner=None, transfer_to_user=None):
    from leads.models import Lead, LeadActivity
    from quality.models import Ticket
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError(gettext("Укажите причину закрытия партнёра."))
    if partner.status == Franchisee.Status.CLOSED:
        raise BusinessError(gettext("Партнёр уже закрыт."))
    if bool(transfer_to_partner) == bool(transfer_to_user):
        raise BusinessError(gettext("Укажите, кому передать клиентов: transfer_to_partner или transfer_to_user."))
    if transfer_to_partner is not None:
        if transfer_to_partner.id == partner.id:
            raise BusinessError(gettext("Нельзя передать клиентов самому себе."))
        if transfer_to_partner.status != Franchisee.Status.FRANCHISEE:
            raise BusinessError(gettext("Принимающий партнёр должен иметь статус «Франчайзи»."))
    if transfer_to_user is not None and transfer_to_user.franchisee_id is not None:
        raise BusinessError(gettext("transfer_to_user — сотрудник OZOK."))
    closed_at = closed_at or today()
    partners.change_partner_field(partner, "status", Franchisee.Status.CLOSED, user=user, reason=reason,
                                  effective_from=closed_at, allow_backdate=True)
    partner.refresh_from_db()
    partner.closed_at, partner.status = closed_at, Franchisee.Status.CLOSED
    partner.save()

    # 1. доступ: пользователи блокируются, токены отзываются
    blocked = list(User.objects.filter(franchisee=partner, is_active=True))
    for u in blocked:
        u.is_active = False
        u.save(update_fields=["is_active"])
        revoke_tokens(u)

    # 2. договоры закрываются (H3); долги по рассрочке остаются в регистре партнёра
    from datetime import timedelta
    for c in Contract.objects.filter(franchisee=partner).exclude(status=Contract.Status.CLOSED):
        c.status = Contract.Status.CLOSED
        c.valid_until = min(c.valid_until or closed_at, closed_at - timedelta(days=1))
        c.save(update_fields=["status", "valid_until", "updated_at"])

    # 3. филиалы: закрепление с датой вступления closed_at (платежи после неё партнёру не начисляются)
    moved = {"branches": 0, "clients": 0, "tickets": 0, "leads": 0}
    to = transfer_to_partner
    for b in Branch.objects.filter(branch_svc.served_by(partner.id) | Q(seller_partner=partner)):
        old = partner
        branch_svc.record(b, field=H.Field.SERVICE, from_partner=old, to_partner=to, reason_code="partner_closed",
                          reason=gettext('Уход партнёра: {reason}').format(reason=reason), user=user, effective_from=closed_at)
        if b.implementer_partner_id == partner.id:
            b.implementer_partner = to
        if b.responsible_partner_id == partner.id:
            b.responsible_partner = to
        if b.seller_partner_id == partner.id:
            b.seller_partner = to
        if b.priority_partner_id == partner.id:
            b.priority_partner = to
        if transfer_to_user is not None:
            b.support_user = transfer_to_user
        b.save()
        moved["branches"] += 1
    # 4. клиенты, обращения, лиды
    if to is not None:
        moved["clients"] = Client.objects.filter(franchisee=partner).update(franchisee=to)
        moved["tickets"] = Ticket.objects.filter(franchisee=partner, status__in=("open", "in_progress")).update(
            franchisee=to)
        for lead in Lead.objects.filter(franchisee=partner, status__in=OPEN_LEAD):
            lead.franchisee = to
            lead.save(update_fields=["franchisee", "updated_at"])
            LeadActivity.objects.create(lead=lead, user=user, type="reassign",
                                        text=gettext('Передано партнёру «{name}»: уход «{name1}»').format(name=to.name, name1=partner.name))
            moved["leads"] += 1
    else:
        moved["tickets"] = Ticket.objects.filter(franchisee=partner, status__in=("open", "in_progress")).update(
            assignee=transfer_to_user)
        for lead in Lead.objects.filter(franchisee=partner, status__in=OPEN_LEAD):
            lead.implementer = transfer_to_user
            lead.save(update_fields=["implementer", "updated_at"])
            LeadActivity.objects.create(lead=lead, user=user, type="reassign",
                                        text=gettext('Передано сотруднику OZOK: уход «{name}»').format(name=partner.name))
            moved["leads"] += 1
    audit.log_event("status", partner, changes={"closed": {"users_blocked": len(blocked), **moved}}, reason=reason,
                    user=user)
    return {"partner": partner, "users_blocked": len(blocked), **moved}

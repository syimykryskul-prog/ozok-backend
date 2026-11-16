"""Бизнес-правила обучения."""
from django.utils.translation import gettext
from django.utils import timezone

from accounts.models import User
from core.exceptions import BusinessError
from core.models import add_months, today

from .models import Enrollment


def enroll(course, user, now=None):
    """Назначить курс сотруднику франчайзи. Провалившему / с истёкшим сертификатом — назначить повторно."""
    if user.franchisee_id is None:
        raise BusinessError(gettext("Обучение назначается сотрудникам франчайзи."))
    existing = Enrollment.objects.filter(course=course, user=user).first()
    now = now or timezone.now()
    if existing is None:
        return Enrollment.objects.create(course=course, user=user, franchisee_id=user.franchisee_id,
                                         assigned_at=now)
    if existing.status == Enrollment.Status.FAILED or existing.expired:
        existing.status = Enrollment.Status.ASSIGNED
        existing.score = existing.completed_at = existing.expires_at = None
        existing.assigned_at = now
        existing.save()
        return existing
    raise BusinessError(gettext("Этот курс уже назначен сотруднику."))


def start(enrollment):
    if enrollment.status != Enrollment.Status.ASSIGNED:
        raise BusinessError(gettext("Начать можно только назначенный курс."))
    enrollment.status = Enrollment.Status.IN_PROGRESS
    enrollment.save()
    return enrollment


def complete(enrollment, score, now=None):
    if enrollment.status not in (Enrollment.Status.ASSIGNED, Enrollment.Status.IN_PROGRESS):
        raise BusinessError(gettext("Результат можно внести только по назначенному или начатому курсу."))
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise BusinessError(gettext("Балл должен быть целым числом от 0 до 100."))
    now = now or timezone.now()
    course = enrollment.course
    enrollment.score = score
    enrollment.completed_at = now
    if score >= course.pass_score:
        enrollment.status = Enrollment.Status.PASSED
        enrollment.expires_at = add_months(timezone.localtime(now).date(), course.validity_months)
    else:
        enrollment.status = Enrollment.Status.FAILED
        enrollment.expires_at = None
    enrollment.save()
    _certify_candidate(enrollment)
    return enrollment


def _certify_candidate(enrollment):
    """P2: результат экзамена кандидата = certification_result; сдал на этапе «экзамен» → менеджер."""
    from network import partners
    from network.models import Franchisee
    partner = enrollment.franchisee
    if partner.funnel_stage != Franchisee.Stage.EXAM or partner.status not in ("candidate", "trainee"):
        return
    result = "passed" if enrollment.status == Enrollment.Status.PASSED else "failed"
    partners.record_certification(partner, result, on=timezone.localtime(enrollment.completed_at).date())


def trained_stats(franchisee_ids=None, on=None):
    """{franchisee_id: (обучено, всего активных сотрудников)}.
    Обучен = есть действующий (expires_at >= сегодня) passed хотя бы по одному курсу."""
    on = on or today()
    users = User.objects.filter(is_active=True, franchisee__isnull=False)
    if franchisee_ids is not None:
        users = users.filter(franchisee_id__in=list(franchisee_ids))
    total = {}
    for fid in users.values_list("franchisee_id", flat=True):
        total[fid] = total.get(fid, 0) + 1
    ok_users = set(Enrollment.objects.filter(
        status=Enrollment.Status.PASSED, expires_at__gte=on, user__in=users).values_list("user_id", flat=True))
    trained = {}
    for uid, fid in users.filter(id__in=ok_users).values_list("id", "franchisee_id"):
        trained[fid] = trained.get(fid, 0) + 1
    ids = franchisee_ids if franchisee_ids is not None else total.keys()
    return {fid: (trained.get(fid, 0), total.get(fid, 0)) for fid in ids}


def trained_share(trained, total):
    return round(trained / total, 2) if total else 0.0

"""Бизнес-правила задач."""
from django.utils.translation import gettext
from django.db import transaction
from django.utils import timezone

from core.exceptions import BusinessError

from .models import Task, TaskComment


def add_comment(task, author, text, now=None):
    return TaskComment.objects.create(task=task, author=author, text=text, created_at=now or timezone.now())


def _move(task, allowed, new_status, message, actor, comment="", now=None, required_comment=False):
    if task.status not in allowed:
        raise BusinessError(message)
    comment = (comment or "").strip()
    if required_comment and not comment:
        raise BusinessError(gettext("Укажите комментарий с причиной отказа."))
    with transaction.atomic():
        task.status = new_status
        if new_status == Task.Status.DONE:
            task.done_at = now or timezone.now()
        task.save()
        if comment:
            add_comment(task, actor, comment, now)
    return task


def start_task(task, actor, comment="", now=None):
    return _move(task, ("new",), "in_progress", gettext("Начать можно только новую задачу."), actor, comment, now)


def done_task(task, actor, comment="", now=None):
    return _move(task, ("new", "in_progress"), "done", gettext("Выполнить можно новую задачу или задачу в работе."),
                 actor, comment, now)


def reject_task(task, actor, comment, now=None):
    return _move(task, ("new", "in_progress"), "rejected", gettext("Отклонить можно новую задачу или задачу в работе."),
                 actor, comment, now, required_comment=True)


def cancel_task(task, actor, now=None):
    return _move(task, ("new", "in_progress"), "cancelled", gettext("Отменить можно только активную задачу."), actor,
                 now=now)

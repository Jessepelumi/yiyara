"""Shared protection for work with an unfinished external calendar commitment."""
from django.db.models import Q
from rest_framework.exceptions import ValidationError
from calendars.models import WorkSession


def active_sessions(task):
    return WorkSession.objects.filter(user_id=task.goal.user_id, finished_at__isnull=True,
        status__in=['publishing', 'published']).filter(Q(task_id=task.pk) | Q(task_snapshot__task_id=str(task.pk)))


def ensure_editable(task, fields=None):
    if (fields is None or set(fields) - {'is_completed'}) and active_sessions(task).exists():
        raise ValidationError('Finish or cancel this task’s committed session before editing or deleting its work.')


def ensure_goal_editable(goal):
    for task in goal.tasks.all():
        ensure_editable(task)

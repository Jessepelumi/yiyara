"""Explicit session completion; never infer task completion from elapsed time."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from tasks.models import Task, TaskOutcome
from tasks.outcomes import OutcomeConflict, record_outcome
from .models import WorkSession
from .services import lock_user


def finish(user, session_id, data):
    with transaction.atomic():
        lock_user(user)
        session = get_object_or_404(WorkSession, pk=session_id, user=user)
        task = get_object_or_404(Task, pk=session.task_snapshot.get('task_id'), goal__user=user)
        # Exact retries remain valid even after later progress or cancellation.
        existing = TaskOutcome.objects.filter(plan=task.goal.plan, client_id=data['client_id']).first()
        if session.finished_at and (not existing or existing.pk != session.finish_outcome_id):
            raise OutcomeConflict('This session is already finished. Report further progress on the task instead.')
        if not session.finished_at and session.status != 'published':
            raise OutcomeConflict('Only a confirmed published session can be finished.')
        task, plan, outcome, replayed = record_outcome(user, task.pk, {**data, 'session_id': session.pk})
        if replayed and session.finish_outcome_id != outcome.pk:
            raise OutcomeConflict('This client_id was already used for a task report. Use a new client_id to finish the session.')
        if not replayed:
            session.finished_at = timezone.now()
            session.finish_outcome_id = outcome.pk
            session.management_version += 1
            session.save(update_fields=['finished_at', 'finish_outcome_id', 'management_version'])
            session.changes.filter(status='proposed').update(status='rejected')
        return session, task, plan, outcome, replayed

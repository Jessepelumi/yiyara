"""User-reported progress, independent of Google event publication and AI inference."""
import uuid

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import APIException

from calendars.models import WorkSession
from calendars.services import lock_user
from goals.models import Plan
from .models import Task, TaskOutcome


class OutcomeConflict(APIException):
    status_code = 409
    default_detail = 'Refresh the plan before recording progress.'


class OutcomeRequestSerializer(serializers.Serializer):
    client_id = serializers.UUIDField()
    plan_version = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(choices=['completed', 'partial', 'blocked', 'skipped', 'reopened'])
    notes = serializers.CharField(max_length=10000, allow_blank=True, default='')
    remaining_minutes = serializers.IntegerField(min_value=0, max_value=480, allow_null=True, default=None)
    spent_minutes = serializers.IntegerField(min_value=0, max_value=1440, allow_null=True, default=None)
    blocker_reason = serializers.CharField(max_length=2000, allow_blank=True, default='')
    session_id = serializers.UUIDField(allow_null=True, default=None)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['Provide only supported outcome fields in a JSON object.']})
        return super().to_internal_value(data)

    def validate(self, attrs):
        status, remaining = attrs['status'], attrs['remaining_minutes']
        if status == 'completed':
            if remaining not in (None, 0):
                raise serializers.ValidationError('Completed work must have zero remaining minutes.')
            attrs['remaining_minutes'] = 0
        elif status in ('partial', 'reopened') and (remaining is None or remaining < 5):
            raise serializers.ValidationError('Partial or reopened work needs 5–480 remaining minutes.')
        elif remaining is not None and remaining < 5:
            raise serializers.ValidationError('Unfinished work needs 5–480 remaining minutes, or null if unknown.')
        if status == 'blocked' and not attrs['blocker_reason'].strip():
            raise serializers.ValidationError('Explain what is blocking this task.')
        if status != 'blocked' and attrs['blocker_reason']:
            raise serializers.ValidationError('Use blocked status when reporting a blocker.')
        return attrs


def progress_state(task):
    return {field: getattr(task, field) for field in (
        'is_completed', 'progress_status', 'remaining_minutes', 'progress_version', 'readiness', 'blocker_reason',
    )}


def apply_report(task, data):
    status = data['status']
    task.is_completed = status == 'completed'
    task.progress_status = 'not_started' if status == 'reopened' else status
    # Null means the remaining work is unknown; never silently subtract time spent.
    task.remaining_minutes = data['remaining_minutes']
    task.blocker_reason = data['blocker_reason']
    task.progress_version += 1
    if status == 'blocked':
        task.readiness = 'blocked'
    elif status == 'skipped':
        task.readiness = 'draft'
    else:
        effort = task.remaining_minutes if task.remaining_minutes is not None else task.estimated_minutes
        task.readiness = 'ready' if task.completion_criteria.strip() and effort and 5 <= effort <= 480 else 'draft'
    task.save(update_fields=[*progress_state(task), 'updated_at'])


def append_report(task, plan, data, *, source='outcome'):
    """Caller holds the shared user lock and the plan transaction."""
    if data.get('session_id') and not WorkSession.objects.filter(
        pk=data['session_id'], user_id=plan.user_id, status='published', task_snapshot__task_id=str(task.pk),
    ).exists():
        raise serializers.ValidationError('The session must be a published session for this task and user.')
    if WorkSession.objects.filter(user_id=plan.user_id, task_snapshot__task_id=str(task.pk), status='publishing').exists():
        raise OutcomeConflict('Calendar publishing is unconfirmed. Retry session approval to reconcile it before reporting progress.')
    apply_report(task, data)
    outcome = TaskOutcome.objects.create(
        plan=plan, task_id=task.pk, task_title=task.title,
        client_id=data.get('client_id') or uuid.uuid4(), status=data['status'], notes=data.get('notes', ''),
        remaining_minutes=task.remaining_minutes, spent_minutes=data.get('spent_minutes'),
        blocker_reason=task.blocker_reason, session_id=data.get('session_id'),
        request_data={**data, 'client_id': str(data.get('client_id', '')), 'session_id': str(data['session_id']) if data.get('session_id') else None,
                      'source': source}, task_state=progress_state(task),
    )
    WorkSession.objects.filter(user_id=plan.user_id, task_snapshot__task_id=str(task.pk), status='proposed').update(status='rejected')
    return outcome


def record_outcome(user, task_id, data):
    from goals.services import create_plan_revision

    with transaction.atomic():
        lock_user(user)
        task = get_object_or_404(Task.objects.select_related('goal__plan'), pk=task_id, goal__user=user)
        plan = Plan.objects.select_for_update().get(pk=task.goal.plan_id)
        canonical = {**data, 'client_id': str(data['client_id']), 'session_id': str(data['session_id']) if data['session_id'] else None, 'source': 'outcome'}
        existing = TaskOutcome.objects.filter(plan=plan, client_id=data['client_id']).first()
        if existing:
            if existing.task_id != task.pk or existing.request_data != canonical:
                raise OutcomeConflict('This client_id belongs to a different outcome. Use a new client_id for a new report.')
            return task, plan, existing, True
        if plan.version != data['plan_version']:
            raise OutcomeConflict()
        if plan.status != 'active':
            raise serializers.ValidationError('Progress reporting requires an active plan.')
        if task.is_completed and data['status'] != 'reopened':
            raise serializers.ValidationError('Reopen the completed task before reporting another outcome.')
        outcome = append_report(task, plan, data)
        plan.version += 1
        plan.save(update_fields=['version', 'updated_at'])
        create_plan_revision(plan, f'Recorded task outcome: {data["status"]}')
        return task, plan, outcome, False


def record_completion_toggle(task, plan, completed):
    """Keep the existing PATCH/apply completion controls compatible and auditable."""
    if task.is_completed == completed:
        return
    remaining = None if completed else (task.estimated_minutes if task.estimated_minutes and task.estimated_minutes >= 5 else None)
    # Legacy draft tasks may have no effort yet; they remain unschedulable.
    status = 'completed' if completed else 'reopened'
    append_report(task, plan, {
        'status': status, 'notes': '', 'remaining_minutes': 0 if completed else remaining,
        'spent_minutes': None, 'blocker_reason': '', 'session_id': None,
    }, source='completion_toggle')


def preserve_reported_progress(task, plan):
    latest = TaskOutcome.objects.filter(plan=plan, task_id=task.pk).first()
    if latest:
        for field, value in latest.task_state.items():
            setattr(task, field, value)
        task.save(update_fields=[*latest.task_state, 'updated_at'])

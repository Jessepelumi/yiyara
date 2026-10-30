"""Read external changes without overwriting Google or inferring task progress."""
from django.conf import settings
import copy
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException

from tasks.models import Task
from . import management, services, sessions
from .models import WorkSession, SessionReconciliation, SchedulingPreferences
from .scheduling import permitted_windows


def token(session):
    return session.management_version, session.synced_at, session.status


def check_idle(session, allow_change=None):
    if session.status not in ('published', 'cancelled') or not session.published_at:
        raise sessions.SessionConflict('Refresh requires a previously published session. Reconcile initial publishing through approval.')
    if session.changes.filter(status='applying').exists():
        raise sessions.SessionConflict('A calendar change is unconfirmed. Retry approval of that change before refreshing.')
    if (session.lease_until and session.lease_until > timezone.now()) or session.changes.filter(
        status='proposed', lease_until__gt=timezone.now(),
    ).exclude(pk=allow_change).exists():
        raise sessions.SessionConflict('Calendar approval is in progress. Retry refresh shortly.')


def summary(event):
    """Store useful event data, excluding attendees and arbitrary private metadata."""
    result = {key: copy.deepcopy(event[key]) for key in (
        'id', 'etag', 'status', 'summary', 'description', 'start', 'end', 'transparency', 'visibility',
    ) if key in event}
    result['recurring'] = bool(event.get('recurrence') or event.get('recurringEventId'))
    result['has_guests'] = bool(event.get('attendees'))
    result['end_unspecified'] = bool(event.get('endTimeUnspecified'))
    result['event_type'] = event.get('eventType', 'default')
    extended = event.get('extendedProperties')
    private = extended.get('private', {}) if isinstance(extended, dict) else {}
    private = private if isinstance(private, dict) else {}
    result['session_marker'] = private.get('yiyara_session_id')
    return result


def local_state(session):
    return {'status': session.status, 'starts_at': session.starts_at.isoformat(),
            'ends_at': session.ends_at.isoformat(), 'event': summary(session.event_payload)}


def observe(user, session_id, expected, code, event, *, allow_change=None):
    """Caller has verified access to the original calendar before reading the event."""
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession, pk=session_id, user=user)
        check_idle(session, allow_change)
        if token(session) != expected:
            raise sessions.SessionConflict('Session changed during refresh. Refresh again before acting.')
        if services.get_connection(user).google_sub != session.google_sub:
            raise sessions.SessionConflict('Calendar account changed during refresh.')
        before = local_state(session)
        issues, kind = [], None
        gone = code in (404, 410) or (code == 200 and event.get('id') == session.google_event_id and event.get('status') == 'cancelled')
        observed = {'id': session.google_event_id, 'status': 'cancelled'} if gone else summary(event)
        if gone:
            if session.status != 'cancelled':
                kind = 'deleted'
                session.status = 'cancelled'
                session.cancelled_at = timezone.now()
            session.sync_status = 'deleted'
        else:
            left = right = None
            try:
                management.managed_event(session, code, event)
                left, right = management.event_times(event)
                if (event.get('endTimeUnspecified') or event.get('eventType', 'default') != 'default'
                        or event.get('status', 'confirmed') not in ('confirmed', 'tentative')):
                    raise sessions.SessionConflict('Unsupported event type or unspecified end time.')
            except APIException:
                issues.append('unsupported_event: event identity, version or timed single-user format could not be verified')
            if not issues and session.status == 'cancelled' and not session.finished_at and WorkSession.objects.filter(
                user=user, task_snapshot__task_id=session.task_snapshot.get('task_id'), finished_at__isnull=True,
                status__in=['publishing', 'published'],
            ).exclude(pk=session.pk).exists():
                issues.append('another_active_session: restored Google event conflicts with another commitment for this task')
            if not issues:
                changed_time = (left, right) != (session.starts_at, session.ends_at)
                if session.status == 'cancelled':
                    kind = 'restored'
                elif changed_time or any(event.get(key, default) != session.event_payload.get(key, default)
                        for key, default in [('summary', ''), ('description', ''),
                                             ('transparency', 'opaque'), ('visibility', 'default')]):
                    kind = 'changed'
                session.status, session.cancelled_at = 'published', None
                session.starts_at, session.ends_at = left, right
                # Preserve the original task snapshot; Google edits do not edit the task.
                for key in ('summary', 'description', 'start', 'end', 'transparency', 'visibility'):
                    if key in event:
                        session.event_payload[key] = copy.deepcopy(event[key])
                    else:
                        session.event_payload.pop(key, None)
                profile = SchedulingPreferences.objects.filter(user=user).first()
                bounded = timedelta(minutes=settings.MIN_SESSION_MINUTES) <= right - left <= timedelta(minutes=settings.MAX_SESSION_MINUTES)
                windows = permitted_windows(user, left, right) if profile and bounded else None
                if not bounded:
                    issues.append(f'unsupported_duration: external session is outside {settings.MIN_SESSION_MINUTES}–{settings.MAX_SESSION_MINUTES} minutes')
                elif windows is None:
                    issues.append('missing_preferences: set scheduling preferences before evaluating this external time')
                elif not any(a <= left and b >= right for a, b in windows):
                    issues.append('outside_availability: external time overlaps grey periods or lies outside scheduling hours')
                task = Task.objects.select_related('goal__plan').filter(pk=session.task_snapshot.get('task_id'), goal__user=user).first()
                due = sessions.deadline(task, profile) if task and profile else None
                if due and right > due:
                    issues.append('after_deadline: external time ends after the task or goal deadline')
                if task and profile and bounded:
                    from goals.scheduling import apply_windows
                    try:
                        ambition_windows = apply_windows(task.goal.plan, profile, left, right, windows or [])
                        if not any(a <= left and b >= right for a, b in ambition_windows):
                            issues.append('outside_ambition_rules: external time violates confirmed ambition timing')
                    except APIException:
                        issues.append('ambition_rules_pending: confirm the saved ambition timing before relying on this session')

                if changed_time:
                    # Prior overlap consent belongs to its original time, never a new Google time.
                    session.conflicts, session.conflict_token, session.overlap_approved = [], '', False
            elif observed != session.remote_snapshot:
                kind = 'unsupported'
            session.sync_status = 'review' if issues else 'current'
        changed = kind is not None or (session.remote_etag and session.remote_etag != observed.get('etag', ''))
        if changed:
            session.management_version += 1
            session.changes.filter(status='proposed').update(status='rejected', last_error='Google event changed; request a fresh proposal.')
        if kind:
            SessionReconciliation.objects.create(session=session, kind=kind, before=before, after=observed, issues=issues)
        session.synced_at, session.sync_error, session.sync_issues = timezone.now(), '', issues
        session.remote_snapshot = observed
        etag = observed.get('etag')
        session.remote_etag = etag if isinstance(etag, str) else ''
        session.save(update_fields=['status', 'cancelled_at', 'starts_at', 'ends_at', 'event_payload', 'management_version',
            'synced_at', 'sync_status', 'sync_error', 'sync_issues', 'remote_snapshot', 'remote_etag',
            'conflicts', 'conflict_token', 'overlap_approved'])
        return session


def read_verified(user, session):
    management.check_account(user, session)
    code, event = management.remote(user, session)
    if code in (404, 410):
        # A missing resource can also mean calendar access was revoked mid-read.
        management.check_account(user, session)
    return code, event


def refresh(user, session_id):
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession, pk=session_id, user=user)
        check_idle(session)
        expected = token(session)
    try:
        code, event = read_verified(user, session)
        return observe(user, session_id, expected, code, event)
    except APIException as error:
        # Never turn an access failure into deletion, or mark an old check fresh.
        with transaction.atomic():
            services.lock_user(user)
            current = WorkSession.objects.get(pk=session_id, user=user)
            if token(current) == expected:
                current.sync_status = 'error'
                current.sync_error = 'Calendar could not be checked. Reconnect or retry; last known event state was retained.'
                current.save(update_fields=['sync_status', 'sync_error'])
        raise

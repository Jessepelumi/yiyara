"""Single-session proposals and recoverable, idempotent Calendar publishing."""
import uuid
from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from tasks.models import Task
from . import services
from .models import CalendarConnection, SchedulingPreferences, WorkSession
from .scheduling import permitted_windows, subtract_periods


class SessionConflict(APIException):
    status_code = 409
    default_detail = 'Session is no longer valid. Reject it and request a new proposal.'


def snapshot(task):
    return {"task_id": str(task.pk), "title": task.title, "description": task.description,
            "completion_criteria": task.completion_criteria, "estimated_minutes": task.estimated_minutes,
            "readiness": task.readiness, "due_date": str(task.due_date) if task.due_date else None,
            "goal_due_date": str(task.goal.due_date) if task.goal.due_date else None,
            "dependencies": sorted(str(pk) for pk in task.dependencies.values_list('pk', flat=True)),
            "blocker_reason": task.blocker_reason, "planning_context_version": task.planning_context_version}


def matches_snapshot(task, saved):
    # Previously created proposals predate dependency/context fields.
    return snapshot(task) == {"dependencies": [], "blocker_reason": "", "planning_context_version": None, **saved}


def ready(task):
    from tasks.dependencies import scheduling_blockers
    blockers = scheduling_blockers(task)
    if blockers:
        raise ValidationError({'task': blockers})


def deadline(task, preferences):
    dates = [d for d in (task.due_date, task.goal.due_date) if d]
    if not dates:
        return None
    return datetime.combine(min(dates) + timedelta(days=1), time.min, ZoneInfo(preferences.timezone))


def destination(user):
    connection = services.get_connection(user)
    if not connection.source_calendars or not connection.destination_calendar:
        raise ValidationError('Select availability calendars and an owned destination calendar first.')
    return connection


def busy_periods(user, start, end, calendars):
    blocks = []
    ids = list(dict.fromkeys(calendars))
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        payload = services.api(user, 'POST', 'freeBusy', json={
            'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'items': [{'id': c} for c in batch],
        })
        results = payload.get('calendars')
        for calendar in batch:
            result = results.get(calendar) if isinstance(results, dict) else None
            if not isinstance(result, dict) or result.get('errors') or not isinstance(result.get('busy'), list):
                raise services.CalendarError('Availability is incomplete. Check selected calendars before scheduling.')
            for block in result['busy']:
                try:
                    left = datetime.fromisoformat(block['start'].replace('Z', '+00:00'))
                    right = datetime.fromisoformat(block['end'].replace('Z', '+00:00'))
                    if left.utcoffset() is None or right.utcoffset() is None or right <= left:
                        raise ValueError()
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise services.CalendarError('Google returned invalid availability; no session was scheduled.') from None
                blocks.append((left, right))
    return blocks


def local_windows(user, start, end, exclude=None):
    windows = permitted_windows(user, start, end)
    if windows is None:
        raise ValidationError('Set scheduling preferences first.')
    reservations = WorkSession.objects.filter(user=user, starts_at__lt=end, ends_at__gt=start).exclude(pk=exclude)
    reservations = [s for s in reservations if s.status in ('publishing', 'published') or
                    (s.status == 'proposed' and s.expires_at > timezone.now())]
    return subtract_periods(windows, [(s.starts_at, s.ends_at) for s in reservations])


def check_destination(user, calendar):
    if not any(c['id'] == calendar and c.get('accessRole') == 'owner' for c in services.list_calendars(user)):
        raise SessionConflict('Destination is no longer an owned calendar. Update calendar preferences.')


def propose(user, task_id, start, end):
    task = get_object_or_404(Task.objects.select_related('goal__plan'), pk=task_id, goal__user=user)
    ready(task)
    if start < timezone.now():
        raise ValidationError('Proposal range must start in the future.')
    connection = destination(user)
    sources, target, subject = list(connection.source_calendars), connection.destination_calendar, connection.google_sub
    original = snapshot(task)
    check_destination(user, target)
    busy = busy_periods(user, start, end, [*sources, target])
    with transaction.atomic():
        services.lock_user(user)
        task = get_object_or_404(Task.objects.select_related('goal__plan'), pk=task_id, goal__user=user)
        ready(task)
        if snapshot(task) != original:
            raise SessionConflict('Task changed while proposing. Request a new proposal.')
        connection = destination(user)
        if (connection.source_calendars, connection.destination_calendar, connection.google_sub) != (sources, target, subject):
            raise SessionConflict('Calendar preferences changed. Request a new proposal.')
        if WorkSession.objects.filter(user=user, task_snapshot__task_id=str(task.pk),
                                      status__in=['publishing', 'published']).exists():
            raise SessionConflict('This task already has a committed session.')
        preferences = SchedulingPreferences.objects.filter(user=user).first()
        if not preferences:
            raise ValidationError('Set scheduling preferences first.')
        due = deadline(task, preferences)
        limit = min(end, due) if due else end
        start = max(start, timezone.now())
        duration = timedelta(minutes=task.estimated_minutes)
        # A replacement proposal may reuse its own previous tentative reservation.
        previous = list(WorkSession.objects.filter(task=task, status='proposed').values_list('pk', flat=True))
        windows = permitted_windows(user, start, limit) if start < limit else []
        reservations = WorkSession.objects.filter(user=user, starts_at__lt=limit, ends_at__gt=start).exclude(pk__in=previous)
        blocks = [(s.starts_at, s.ends_at) for s in reservations if s.status in ('publishing', 'published') or
                  (s.status == 'proposed' and s.expires_at > timezone.now())]
        windows = subtract_periods(windows or [], [*busy, *blocks])
        slot = None
        for left, right in windows:
            if left.second or left.microsecond:
                left = left.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if left + duration <= right:
                slot = (left, left + duration)
                break
        if not slot:
            raise SessionConflict('No continuous slot fits the task before its deadline. Adjust the range, effort, or availability.')
        WorkSession.objects.filter(pk__in=previous).update(status='rejected')
        session_id = uuid.uuid4()
        event_id = session_id.hex
        payload = {
            'id': event_id, 'summary': task.title,
            'description': task.description + '\n\nDone when: ' + task.completion_criteria,
            'start': {'dateTime': slot[0].isoformat()}, 'end': {'dateTime': slot[1].isoformat()},
            'transparency': 'opaque', 'visibility': 'private',
            'extendedProperties': {'private': {'yiyara_session_id': str(session_id)}},
        }
        return WorkSession.objects.create(
            id=session_id, user=user, task=task, starts_at=slot[0], ends_at=slot[1],
            expires_at=min(slot[0], timezone.now() + timedelta(hours=24)), destination_calendar=target,
            google_sub=subject, google_event_id=event_id, task_snapshot=original, event_payload=payload,
        )


def _event_request(user, session, method, payload, write_state):
    token = services.access_token(user)
    path = 'calendars/' + quote(session.destination_calendar, safe='') + '/events'
    if method == 'GET':
        path += '/' + session.google_event_id
    kwargs = {'headers': {'Authorization': 'Bearer ' + token}}
    if payload is not None:
        kwargs['json'] = payload
    if method == 'POST':
        # Keep local constraint edits serialized through the final provider write.
        # Intent was already committed by _approve, so a timeout here stays recoverable.
        with transaction.atomic():
            services.lock_user(user)
            if not WorkSession.objects.filter(
                pk=session.pk, publish_attempt=session.publish_attempt,
                lease_until__gt=timezone.now(), status='publishing'
            ).exists():
                raise SessionConflict('Approval attempt expired. Retry approval of this same session.')
            connection = destination(user)
            if (connection.google_sub != session.google_sub
                    or connection.destination_calendar != session.destination_calendar):
                raise SessionConflict('Calendar connection changed before publishing.')
            task = Task.objects.select_related('goal__plan').filter(pk=session.task_id).first()
            if not task:
                raise SessionConflict('Task no longer exists.')
            ready(task)
            if not matches_snapshot(task, session.task_snapshot):
                raise SessionConflict('Task changed before publishing.')
            preferences = SchedulingPreferences.objects.get(user=user)
            due = deadline(task, preferences)
            if session.starts_at <= timezone.now() or (due and session.ends_at > due):
                raise SessionConflict('Session is past its permitted time.')
            busy = busy_periods(user, session.starts_at, session.ends_at,
                                [*connection.source_calendars, session.destination_calendar])
            windows = subtract_periods(local_windows(user, session.starts_at, session.ends_at, session.pk), busy)
            if not any(a <= session.starts_at and b >= session.ends_at for a, b in windows):
                raise SessionConflict('This slot became busy or protected before publishing.')
            if not WorkSession.objects.filter(pk=session.pk, publish_attempt=session.publish_attempt,
                                              lease_until__gt=timezone.now()).exists():
                raise SessionConflict('Approval attempt expired. Retry this same session.')
            write_state['started'] = True
            status, result = services.request_json(method, services.API_URL + path, **kwargs)
    else:
        status, result = services.request_json(method, services.API_URL + path, **kwargs)
    if status == 401:
        CalendarConnection.objects.filter(user=user).update(reconnect_required=True)
        raise services.ReconnectRequired()
    if status not in (200, 201, 404, 409):
        raise services.CalendarError('Calendar publishing could not be confirmed. Retry approval of this same session.')
    return status, result


def event_request(user, session, method, payload=None):
    write_state = {'started': False}
    try:
        return _event_request(user, session, method, payload, write_state)
    except APIException as error:
        if method == 'POST' and not write_state['started']:
            error.before_calendar_insert = True
        raise


def confirm_event(session, event):
    try:
        valid = (event.get('id') == session.google_event_id and event.get('status') != 'cancelled'
                 and event.get('extendedProperties', {}).get('private', {}).get('yiyara_session_id') == str(session.pk)
                 and datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00')) == session.starts_at
                 and datetime.fromisoformat(event['end']['dateTime'].replace('Z', '+00:00')) == session.ends_at)
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise SessionConflict('Existing Google event differs from the approved session. No new event was created.')
    WorkSession.objects.filter(pk=session.pk).update(status='published', published_at=timezone.now(), last_error='')
    session.refresh_from_db()
    return session


def _approve(user, session_id):
    session = get_object_or_404(WorkSession, pk=session_id, user=user)
    if session.status == 'published':
        return session
    if session.status == 'rejected':
        raise SessionConflict('Rejected sessions cannot be approved.')
    connection = destination(user)
    if connection.google_sub != session.google_sub:
        raise SessionConflict('Reconnect the Google account used for this proposal.')
    # Reconcile an uncertain previous write BEFORE checking busy time (our event may already exist).
    if session.status == 'publishing':
        status, event = event_request(user, session, 'GET')
        if status == 200:
            return confirm_event(session, event)
        if status != 404:
            raise services.CalendarError('Unable to reconcile publishing. Retry this session.')
    sources, target = list(connection.source_calendars), connection.destination_calendar
    check_destination(user, session.destination_calendar)
    busy = busy_periods(user, session.starts_at, session.ends_at, [*sources, session.destination_calendar])
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession.objects.select_for_update(), pk=session_id, user=user)
        if session.status == 'published':
            return session
        if session.status == 'rejected':
            raise SessionConflict('Rejected sessions cannot be approved.')
        connection = destination(user)
        if (connection.source_calendars, connection.destination_calendar, connection.google_sub) != (sources, target, session.google_sub):
            raise SessionConflict('Calendar selection changed. Request a new proposal.')
        if target != session.destination_calendar:
            raise SessionConflict('Destination changed. Request a new proposal.')
        if not session.task_id:
            raise SessionConflict('The original task no longer exists.')
        task = Task.objects.select_related('goal__plan').get(pk=session.task_id)
        ready(task)
        if not matches_snapshot(task, session.task_snapshot):
            raise SessionConflict('Task changed. Request a new proposal.')
        preferences = SchedulingPreferences.objects.filter(user=user).first()
        if not preferences:
            raise ValidationError('Set scheduling preferences first.')
        due = deadline(task, preferences)
        if due and session.ends_at > due:
            raise SessionConflict('Session would now exceed the task deadline.')
        now = timezone.now()
        if session.starts_at <= now or (session.status == 'proposed' and session.expires_at <= now):
            raise SessionConflict('Proposal expired or session has already started.')
        windows = subtract_periods(local_windows(user, session.starts_at, session.ends_at, exclude=session.pk), busy)
        if not any(left <= session.starts_at and right >= session.ends_at for left, right in windows):
            raise SessionConflict('This slot is now busy or protected. Request a new proposal.')
        first_attempt = session.status == 'proposed'
        session.status = 'publishing'
        session.approved_at = session.approved_at or now
        session.last_error = ''
        session.save(update_fields=['status', 'approved_at', 'last_error'])
    # The intent is committed before contacting Google. Retries always reuse the event ID and payload.
    try:
        status, event = event_request(user, session, 'POST', session.event_payload)
        if status == 409:
            status, event = event_request(user, session, 'GET')
        if status not in (200, 201):
            raise services.CalendarError('Event not confirmed. Retry approval of this same session.')
        return confirm_event(session, event)
    except APIException as error:
        if isinstance(error, services.ReconnectRequired):
            CalendarConnection.objects.filter(user=user).update(reconnect_required=True)
        updates = {'last_error': 'Publishing is unconfirmed. Retry approval of this same session to reconcile.'}
        if first_attempt and getattr(error, 'before_calendar_insert', False):
            updates = {'status': 'proposed', 'approved_at': None,
                       'last_error': 'No event was submitted. Review this proposal before retrying.'}
        WorkSession.objects.filter(pk=session.pk, status='publishing').update(**updates)
        raise


def approve(user, session_id):
    # An expiring lease prevents simultaneous retries from submitting concurrent inserts.
    # A crashed process leaves a recoverable publishing intent, not a permanently locked session.
    attempt = uuid.uuid4()
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession.objects.select_for_update(), pk=session_id, user=user)
        if session.status == 'published':
            return session
        if session.status == 'rejected':
            raise SessionConflict('Rejected sessions cannot be approved.')
        if session.lease_until and session.lease_until > timezone.now():
            raise SessionConflict('Approval is already in progress. Retry this same session shortly.')
        session.publish_attempt = attempt
        session.lease_until = timezone.now() + timedelta(minutes=2)
        session.save(update_fields=['publish_attempt', 'lease_until'])
    try:
        return _approve(user, session_id)
    finally:
        WorkSession.objects.filter(pk=session_id, publish_attempt=attempt).update(publish_attempt=None, lease_until=None)


def reject(user, session_id):
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession, pk=session_id, user=user)
        if session.status not in ('proposed', 'rejected'):
            raise SessionConflict('Publishing has started; reject cannot cancel a possibly created event.')
        session.status = 'rejected'
        session.save(update_fields=['status'])
        return session

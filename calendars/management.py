"""Reviewable changes to existing events, with durable intent and conditional writes."""
from django.conf import settings
import copy
import uuid
from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from tasks.models import Task
from . import services, sessions, conflicts
from .models import CalendarConnection, SchedulingPreferences, SessionChange, WorkSession

Conflict = sessions.SessionConflict


def check_account(user, session):
    connection = services.get_connection(user)
    if connection.google_sub != session.google_sub:
        raise Conflict('Reconnect the Google account that owns this session.')
    sessions.check_destination(user, session.destination_calendar)
    return connection


def event_path(session):
    return 'calendars/' + quote(session.destination_calendar, safe='') + '/events/' + quote(session.google_event_id, safe='')


def remote(user, session, method='GET', *, payload=None, etag=None, token=None):
    headers = {'Authorization': 'Bearer ' + (token if token is not None else services.access_token(user))}
    if etag:
        headers['If-Match'] = etag
    kwargs = {'headers': headers}
    if payload is not None:
        kwargs['json'] = payload
    code, event = services.request_json(method, services.API_URL + event_path(session), **kwargs)
    if code == 401:
        CalendarConnection.objects.filter(user=user).update(reconnect_required=True)
        error = services.ReconnectRequired()
    elif code in (200, 204, 404, 410):
        return code, event
    elif code == 412:
        error = Conflict('Google event changed. Review it and request a new proposal.')
    else:
        error = services.CalendarError('Calendar change is unconfirmed. Retry approval of this same change.')
    error.no_calendar_mutation = 400 <= code < 500
    raise error


def missing(code, event):
    return code in (404, 410) or (code == 200 and event.get('status') == 'cancelled')


def managed_event(session, code, event):
    extended = event.get('extendedProperties')
    private = extended.get('private') if isinstance(extended, dict) else None
    if (code != 200 or event.get('id') != session.google_event_id or not isinstance(private, dict)
            or private.get('yiyara_session_id') != str(session.pk)):
        raise Conflict('This is not the original Yiyara event. No calendar change was made.')
    if (event.get('recurrence') or event.get('recurringEventId') or event.get('attendees')
            or event.get('endTimeUnspecified') or event.get('eventType', 'default') != 'default'):
        raise Conflict('Only timed, non-recurring, default events without guests are supported by session management.')
    event_times(event)
    if not isinstance(event.get('etag'), str) or not event['etag']:
        raise services.CalendarError('Google returned no event version; no change was made.')


def event_times(event):
    try:
        values = [datetime.fromisoformat(event[key]['dateTime'].replace('Z', '+00:00')) for key in ('start', 'end')]
        if any(value.utcoffset() is None for value in values) or values[1] <= values[0]:
            raise ValueError()
        return tuple(values)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Conflict('The event no longer has the expected timed-session format.') from None


def event_summary(event):
    return {key: copy.deepcopy(event[key]) for key in ('id', 'etag', 'summary', 'description', 'start', 'end', 'extendedProperties') if key in event}


def editable_task(session, saved=None):
    if not timedelta(minutes=settings.MIN_SESSION_MINUTES) <= session.ends_at - session.starts_at <= timedelta(minutes=settings.MAX_SESSION_MINUTES):
        raise Conflict('External session duration is unsupported. Correct it in Google or cancel the session.')
    task = Task.objects.select_related('goal__plan').filter(pk=session.task_id, goal__user_id=session.user_id).first()
    if not task:
        raise Conflict('The original task no longer exists. The event can still be cancelled.')
    sessions.ready(task)
    if not sessions.matches_snapshot(task, session.task_snapshot):
        raise Conflict('Task progress or details changed after publishing. Cancel this session before planning different work.')
    if saved is not None and sessions.snapshot(task) != saved:
        raise Conflict('Task changed after the reschedule proposal. Request a new proposal.')
    return task


def destination_busy(user, session, start, end):
    """Read individual events, never subtract an interval from merged free/busy data."""
    page, blocks = None, []
    for _ in range(100):
        params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'singleEvents': True,
                  'showDeleted': False, 'maxResults': 2500}
        if page:
            params['pageToken'] = page
        result = services.api(user, 'GET', 'calendars/' + quote(session.destination_calendar, safe='') + '/events', params=params)
        if not isinstance(result.get('items', []), list):
            raise services.CalendarError('Destination availability is incomplete.')
        for event in result.get('items', []):
            if not isinstance(event, dict):
                raise services.CalendarError('Destination availability is invalid.')
            if event.get('id') == session.google_event_id or event.get('status') == 'cancelled' or event.get('transparency') == 'transparent':
                continue
            try:
                if 'date' in event['start']:
                    zone = ZoneInfo(result['timeZone'])
                    left = datetime.combine(datetime.fromisoformat(event['start']['date']).date(), time.min, zone)
                    right = datetime.combine(datetime.fromisoformat(event['end']['date']).date(), time.min, zone)
                    if right <= left:
                        raise ValueError()
                else:
                    left, right = event_times(event)
            except (KeyError, TypeError, ValueError, Conflict):
                raise services.CalendarError('Destination availability is incomplete; no session was moved.') from None
            blocks.append((left, right))
        page = result.get('nextPageToken')
        if not page:
            return blocks
    raise services.CalendarError('Too many destination events to safely check availability.')


def availability(user, session, start, end, exclude_change=None):
    connection = services.get_connection(user)
    if not connection.source_calendars:
        raise ValidationError('Select availability calendars before rescheduling.')
    others = [pk for pk in connection.source_calendars if pk != session.destination_calendar]
    busy = sessions.busy_details(user, start, end, others) + [
        {'calendar_id': session.destination_calendar, 'start': left, 'end': right}
        for left, right in destination_busy(user, session, start, end)]
    allowed, local_busy = conflicts.local_availability(user, start, end, exclude_sessions=[session.pk], exclude_change=exclude_change)
    from goals.scheduling import apply_windows
    task = Task.objects.select_related('goal__plan').get(pk=session.task_id)
    profile = SchedulingPreferences.objects.get(user=user)
    allowed = apply_windows(task.goal.plan, profile, start, end, allowed)
    return allowed, [*busy, *local_busy]


def check_slot(user, session, change):
    task = editable_task(session, change.task_snapshot)
    now = timezone.now()
    if session.starts_at <= now or change.starts_at <= now:
        raise Conflict('Only future sessions can be rescheduled.')
    preferences = SchedulingPreferences.objects.filter(user=user).first()
    if not preferences:
        raise ValidationError('Set scheduling preferences before rescheduling.')
    due = sessions.deadline(task, preferences)
    if due and change.ends_at > due:
        raise Conflict('The new session would exceed the task deadline.')
    allowed, details = availability(user, session, change.starts_at, change.ends_at, change.pk)
    if not any(left <= change.starts_at and right >= change.ends_at for left, right in allowed):
        raise Conflict('This time is protected or reserved by an in-flight operation; it cannot be overridden.')
    conflicts.check_review(change, conflicts.reviewed(details, change.starts_at, change.ends_at))


def replay(user, session_id, canonical, client_id):
    change = SessionChange.objects.filter(user=user, client_id=client_id).first()
    if change and (change.session_id != session_id or change.request_data != canonical):
        raise Conflict('This client_id belongs to a different calendar change.')
    return change


def propose(user, session_id, kind, data):
    canonical = {'kind': kind, **{key: value.isoformat() if isinstance(value, datetime) else str(value)
                                  for key, value in data.items() if key != 'client_id'}}
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession, pk=session_id, user=user)
        existing = replay(user, session_id, canonical, data['client_id'])
        if existing:
            return existing, True
        if kind == 'reschedule' and session.finished_at:
            raise Conflict('Finished sessions cannot be rescheduled. Propose a continuation for remaining work.')
        if session.status != 'published':
            raise Conflict('Only a published session can be moved or cancelled. Reconcile initial publishing first.')
        if session.changes.filter(status='applying').exists():
            raise Conflict('An earlier calendar change is unconfirmed. Retry approval of that same change.')
        version = session.management_version
    from . import reconciliation
    code, event = reconciliation.read_verified(user, session)
    session = reconciliation.observe(user, session.pk, reconciliation.token(session), code, event)
    if session.management_version != version:
        raise Conflict('Google changes were reconciled. Review the refreshed session and request a new proposal.')
    gone = missing(code, event)
    if not gone:
        managed_event(session, code, event)
    elif kind == 'reschedule':
        raise Conflict('The Google event no longer exists. Cancel its local session; it will not be recreated.')
    with transaction.atomic():
        services.lock_user(user)
        session = get_object_or_404(WorkSession, pk=session_id, user=user)
        existing = replay(user, session_id, canonical, data['client_id'])
        if existing:
            return existing, True
        if session.status != 'published' or session.management_version != version or session.changes.filter(status='applying').exists():
            raise Conflict('Session changed while proposing. Refresh and retry.')
        old = session.changes.filter(status='proposed').first()
        if old and old.lease_until and old.lease_until > timezone.now():
            raise Conflict('Approval of the current proposal is in progress. Retry shortly.')
        left = right = None
        occupied = []
        saved = {}
        if kind == 'reschedule':
            task = editable_task(session)
            if session.starts_at <= timezone.now() or data['time_min'] <= timezone.now():
                raise ValidationError('Rescheduling requires future sessions and a future search range.')
            if event_times(event) != (session.starts_at, session.ends_at):
                raise Conflict('The event changed during proposal. Refresh the session and request a new proposal.')
            profile = SchedulingPreferences.objects.filter(user=user).first()
            if not profile:
                raise ValidationError('Set scheduling preferences first.')
            due = sessions.deadline(task, profile)
            limit = min(data['time_max'], due) if due else data['time_max']
            duration = session.ends_at - session.starts_at
            if data['time_min'] >= limit:
                raise Conflict('The search range is after the task deadline.')
            preferred = data.get('preferred_start')
            if preferred is not None and preferred + duration > limit:
                raise ValidationError('The preferred session must fit within the search range and deadline.')
            choices, details = availability(user, session, data['time_min'], limit, old.pk if old else None)
            left, right = conflicts.select_slot(choices, details, duration, preferred=preferred, policy=data.get('conflict_policy', 'ask'))
            occupied = conflicts.reviewed(details, left, right)
            if (left, right) == (session.starts_at, session.ends_at):
                raise Conflict('The first fitting slot is the current time. Choose a different search range.')
            saved = sessions.snapshot(task)
        if old:
            old.status = 'rejected'
            old.save(update_fields=['status'])
        expiry = min(timezone.now() + timedelta(seconds=settings.PROPOSAL_TTL_SECONDS), left) if left else timezone.now() + timedelta(seconds=settings.PROPOSAL_TTL_SECONDS)
        change = SessionChange.objects.create(session=session, user=user, client_id=data['client_id'], kind=kind,
            base_version=version, request_data=canonical, before_event={'missing': True} if gone else event_summary(event),
            task_snapshot=saved, starts_at=left, ends_at=right, expires_at=expiry, conflicts=occupied,
            conflict_token=conflicts.fingerprint(occupied, left, right))
        return change, False


def leased(change, attempt):
    if change.attempt != attempt or not change.lease_until or change.lease_until <= timezone.now():
        raise Conflict('Calendar approval attempt expired. Retry this same change.')


def finalize(session, change, event=None):
    if session.management_version != change.base_version:
        raise Conflict('Session changed since this operation was approved.')
    if change.kind == 'cancel':
        session.status = 'cancelled'
        session.cancelled_at = timezone.now()
    else:
        session.starts_at, session.ends_at = change.starts_at, change.ends_at
        session.conflicts, session.conflict_token, session.overlap_approved = change.conflicts, change.conflict_token, change.overlap_approved
        conflicts.record_consent(session, change.pk)
        session.event_payload = {**session.event_payload, 'start': copy.deepcopy(event['start']),
            'end': copy.deepcopy(event['end']), 'extendedProperties': copy.deepcopy(event['extendedProperties'])}
    from .reconciliation import summary
    session.synced_at, session.sync_error, session.sync_issues = timezone.now(), '', []
    session.sync_status = 'deleted' if change.kind == 'cancel' else 'current'
    session.remote_snapshot = summary(event) if event else {'id': session.google_event_id, 'status': 'cancelled'}
    session.remote_etag = session.remote_snapshot.get('etag', '')
    session.management_version += 1
    session.last_error = ''
    session.save(update_fields=['status', 'cancelled_at', 'starts_at', 'ends_at', 'event_payload', 'management_version', 'last_error',
                                'conflicts', 'conflict_token', 'overlap_approved', 'overlap_history',
                                'synced_at', 'sync_status', 'sync_error', 'sync_issues', 'remote_snapshot', 'remote_etag'])
    change.status = 'applied'
    change.applied_at = timezone.now()
    change.last_error = ''
    change.save(update_fields=['status', 'applied_at', 'last_error'])
    return change


def confirms_move(session, change, code, event):
    if code != 200 or event.get('status') == 'cancelled':
        return False
    managed_event(session, code, event)
    return (event.get('extendedProperties', {}).get('private', {}).get('yiyara_change_id') == str(change.pk)
            and event_times(event) == (change.starts_at, change.ends_at))


def conflicted(change):
    # A new remote version (or a deleted resource) fences every old If-Match write.
    # Do not overwrite or claim that a later external edit matches this approval.
    change.status = 'conflicted'
    change.last_error = 'Google event changed outside this approved operation. Review it; cancellation can be proposed separately.'
    change.save(update_fields=['status', 'last_error'])
    return change


def _approve(user, change_id, attempt, allow_overlap=False, conflict_token=''):
    change = SessionChange.objects.select_related('session').get(pk=change_id, user=user)
    session = change.session
    from . import reconciliation
    code, event = reconciliation.read_verified(user, session)
    first = change.status == 'proposed'
    if first:
        observed = reconciliation.observe(user, session.pk, reconciliation.token(session), code, event, allow_change=change.pk)
        if observed.management_version != change.base_version:
            raise Conflict('Google changes were reconciled. Review the refreshed session and request a new proposal.')
    with transaction.atomic():
        services.lock_user(user)
        change = SessionChange.objects.select_for_update().get(pk=change_id, user=user)
        session = WorkSession.objects.select_for_update().get(pk=change.session_id)
        leased(change, attempt)
        if change.status == 'applied':
            return change
        if change.status not in ('proposed', 'applying'):
            raise Conflict('The proposal was rejected or superseded before approval.')
        if session.status != 'published' or session.management_version != change.base_version:
            raise Conflict('The published session changed. Request a new proposal.')
        if change.status == 'proposed' and change.expires_at <= timezone.now():
            raise Conflict('Proposal expired. Request a new proposal.')
        if change.status == 'applying' and change.kind == 'reschedule' and not missing(code, event):
            try:
                confirmed = confirms_move(session, change, code, event)
            except Conflict:
                confirmed = False
            if confirmed:
                return finalize(session, change, event)
        if missing(code, event):
            if change.kind == 'cancel':
                change.approved_at = change.approved_at or timezone.now()
                change.save(update_fields=['approved_at'])
                return finalize(session, change)
            if change.status == 'applying':
                return conflicted(change)
            raise Conflict('The Google event was deleted. No replacement will be created.')
        if change.status == 'applying' and code == 200 and event.get('etag') and event['etag'] != change.before_event.get('etag'):
            return conflicted(change)
        managed_event(session, code, event)
        if event['etag'] != change.before_event.get('etag'):
            raise Conflict('Google event changed since proposal. No stale update was submitted.')
        if change.kind == 'reschedule':
            if change.status == 'proposed':
                conflicts.consent(change, allow_overlap, conflict_token)
            if event_times(event) != (session.starts_at, session.ends_at):
                raise Conflict('The original event time changed outside Yiyara.')
            check_slot(user, session, change)
        change.status = 'applying'
        change.approved_at = change.approved_at or timezone.now()
        change.save(update_fields=['status', 'approved_at', 'overlap_approved'])
    # The approved intent is durable before the provider write; no retry ever inserts an event.
    write_state = {'started': False}
    try:
        token = services.access_token(user)
        with transaction.atomic():
            services.lock_user(user)
            change = SessionChange.objects.select_for_update().get(pk=change_id, user=user)
            session = WorkSession.objects.select_for_update().get(pk=change.session_id)
            leased(change, attempt)
            if change.status != 'applying' or session.status != 'published' or session.management_version != change.base_version:
                raise Conflict('The approved calendar intent changed.')
            check_account(user, session)
            if change.kind == 'reschedule':
                check_slot(user, session, change)
                private = copy.deepcopy(change.before_event.get('extendedProperties', {}).get('private', {}))
                private['yiyara_change_id'] = str(change.pk)
                payload = {'start': {'dateTime': change.starts_at.isoformat()}, 'end': {'dateTime': change.ends_at.isoformat()},
                           'extendedProperties': {'private': private}}
                method = 'PATCH'
            else:
                payload, method = None, 'DELETE'
            leased(change, attempt)
            write_state['started'] = True
            code, result = remote(user, session, method, payload=payload, etag=change.before_event['etag'], token=token)
            if change.kind == 'cancel' and (code == 204 or missing(code, result)):
                return finalize(session, change)
            if change.kind == 'reschedule' and confirms_move(session, change, code, result):
                return finalize(session, change, result)
            error = services.CalendarError('Calendar change is unconfirmed. Retry approval of this same change.')
            error.no_calendar_mutation = code in (404, 410)
            raise error
    except APIException as error:
        fields = {'last_error': 'Calendar change is unconfirmed. Retry approval of this same change.'}
        if first and (not write_state['started'] or getattr(error, 'no_calendar_mutation', False)):
            fields.update(status='proposed', approved_at=None,
                          overlap_approved=False,
                          last_error='No change was submitted. Review this proposal before retrying.')
        SessionChange.objects.filter(pk=change_id, status='applying', attempt=attempt).update(**fields)
        raise


def approve(user, change_id, *, allow_overlap=False, conflict_token=''):
    attempt = uuid.uuid4()
    with transaction.atomic():
        services.lock_user(user)
        change = get_object_or_404(SessionChange.objects.select_for_update(), pk=change_id, user=user)
        if change.status in ('applied', 'conflicted'):
            return change
        if change.status == 'rejected':
            raise Conflict('Rejected changes cannot be approved.')
        if change.lease_until and change.lease_until > timezone.now():
            raise Conflict('Approval is already in progress. Retry this same change shortly.')
        change.attempt, change.lease_until = attempt, timezone.now() + timedelta(seconds=settings.CALENDAR_APPROVAL_LEASE_SECONDS)
        change.save(update_fields=['attempt', 'lease_until'])
    try:
        return _approve(user, change_id, attempt, allow_overlap, conflict_token)
    finally:
        SessionChange.objects.filter(pk=change_id, attempt=attempt).update(attempt=None, lease_until=None)


def reject(user, change_id):
    with transaction.atomic():
        services.lock_user(user)
        change = get_object_or_404(SessionChange, pk=change_id, user=user)
        if change.status not in ('proposed', 'rejected'):
            raise Conflict('An approved or uncertain calendar write cannot be rejected; retry its approval to reconcile it.')
        if change.lease_until and change.lease_until > timezone.now():
            raise Conflict('Approval is already in progress.')
        change.status = 'rejected'
        change.save(update_fields=['status'])
        return change

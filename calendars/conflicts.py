"""Automatic free-slot selection and explicit user-reviewed personal exceptions."""
import hashlib
import json
from datetime import timedelta, timezone as utc_timezone

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import SessionChange, WorkSession
from .scheduling import permitted_windows, subtract_periods


def local_availability(user, start, end, *, exclude_sessions=(), exclude_change=None, manual=False):
    allowed = permitted_windows(user, start, end, ignore_grey=manual)
    if allowed is None:
        raise ValidationError('Set scheduling preferences first.')
    changes = SessionChange.objects.filter(user=user).exclude(pk=exclude_change)
    pending = set(changes.filter(status='applying').values_list('session_id', flat=True))
    hard, occupancy = [], []
    for session in WorkSession.objects.filter(user=user, starts_at__lt=end, ends_at__gt=start).exclude(pk__in=exclude_sessions):
        if session.status == 'published' and session.pk not in pending:
            if session.event_payload.get('transparency') == 'transparent':
                continue
            occupancy.append({'calendar_id': session.destination_calendar, 'start': session.starts_at, 'end': session.ends_at})
        elif session.status == 'publishing' or session.pk in pending or (session.status == 'proposed' and session.expires_at > timezone.now()):
            hard.append((session.starts_at, session.ends_at))
    for change in changes.filter(kind='reschedule', starts_at__lt=end, ends_at__gt=start):
        if change.status == 'applying' or (change.status == 'proposed' and change.expires_at > timezone.now()):
            hard.append((change.starts_at, change.ends_at))
    return subtract_periods(allowed, hard), occupancy


def reviewed(details, start, end):
    grouped, grey = {}, []
    for item in details:
        left, right = max(start, item['start']), min(end, item['end'])
        if left < right:
            if item.get('kind') == 'grey_period':
                grey.append({**item, 'start': left.astimezone(utc_timezone.utc).isoformat(), 'end': right.astimezone(utc_timezone.utc).isoformat()})
            else:
                grouped.setdefault(item['calendar_id'], []).append((left, right))
    result = []
    for calendar, intervals in sorted(grouped.items()):
        merged = []
        for left, right in sorted(intervals):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        result.extend({'calendar_id': calendar, 'start': left.astimezone(utc_timezone.utc).isoformat(),
                       'end': right.astimezone(utc_timezone.utc).isoformat()} for left, right in merged)
    return result + sorted(grey, key=lambda item: (item['period_id'] or 0, item['start'], item['end']))


def fingerprint(items, start, end):
    if not items:
        return ''
    data = {'start': start.astimezone(utc_timezone.utc).isoformat(), 'end': end.astimezone(utc_timezone.utc).isoformat(), 'conflicts': items}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def select_slot(allowed, details, duration, *, preferred=None, policy='ask'):
    from .sessions import SessionConflict

    def first_fit(windows):
        for left, right in windows:
            if left.second or left.microsecond:
                left = left.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if left + duration <= right:
                return left, left + duration
        return None

    if preferred is not None:
        preferred_slot = (preferred, preferred + duration)
        if not any(left <= preferred and preferred + duration <= right for left, right in allowed):
            raise SessionConflict('The selected time violates activity constraints or an in-flight reservation. Choose another time.')
        if policy == 'avoid' and reviewed(details, *preferred_slot):
            raise SessionConflict('The selected time has conflicts. Use ask to review that exact time, or choose another time.')
        return preferred_slot
    free = subtract_periods(allowed, [(item['start'], item['end']) for item in details])
    slot = first_fit(free)
    if slot is None:
        raise SessionConflict('No suitable free slot fits. Widen the range or explicitly select a time to review conflicts.')
    return slot


def consent(record, allow_overlap=False, token=''):
    from .sessions import SessionConflict

    if record.conflicts:
        if record.time_selection != 'manual':
            raise SessionConflict('Only a user-selected time can override personal conflicts. Request a new proposal with preferred_start.')
        if not allow_overlap or token != record.conflict_token:
            raise SessionConflict('This time is occupied. Choose another slot or explicitly approve overlap using the proposal conflict_token.')
        record.overlap_approved = True


def record_consent(session, source_id):
    session.grey_exceptions = [{'starts_at': session.starts_at.astimezone(utc_timezone.utc).isoformat(), 'ends_at': session.ends_at.astimezone(utc_timezone.utc).isoformat(), 'conflict': item}
        for item in session.conflicts if item.get('kind') == 'grey_period' and session.overlap_approved]
    if session.conflicts and session.overlap_approved and not any(item['source_id'] == str(source_id) for item in session.overlap_history):
        session.overlap_history = [*session.overlap_history, {'source_id': str(source_id),
            'approved_at': timezone.now().isoformat(), 'conflict_token': session.conflict_token,
            'conflicts': session.conflicts, 'starts_at': session.starts_at.isoformat(), 'ends_at': session.ends_at.isoformat()}]


def check_review(record, current):
    from .sessions import SessionConflict

    # A user can approve only the busy windows they actually reviewed. Changed/new
    # occupancy requires a new proposal; freed windows require no extra consent.
    if current and (not record.overlap_approved or not record.conflicts or
                    fingerprint(current, record.starts_at, record.ends_at) != record.conflict_token):
        raise SessionConflict('Calendar occupancy changed or overlap was not approved. Review a new proposal before publishing.')


def has_grey_exception(session, item):
    return any(saved.get('starts_at') == session.starts_at.astimezone(utc_timezone.utc).isoformat()
               and saved.get('ends_at') == session.ends_at.astimezone(utc_timezone.utc).isoformat()
               and saved.get('conflict') == item for saved in session.grey_exceptions)

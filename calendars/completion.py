"""Completion appearance for owned events; progress remains authoritative locally."""
import copy
from functools import partial

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from . import management, reconciliation, services
from .models import WorkSession


PREFIX = '✓ Completed: '


def queue_task(user, task):
    """Persist retryable intent in the same transaction as the progress report."""
    for session in WorkSession.objects.filter(user=user, task=task, status='published'):
        state = dict(session.completion_presentation)
        state.update(status='pending', error='')
        session.completion_presentation = state
        session.save(update_fields=['completion_presentation'])
        transaction.on_commit(partial(sync_session, user, session.pk))


def sync_session(user, session_id):
    """ETag-guarded, idempotent update; refresh retries failed or legacy updates.

    The user lock serializes progress changes and other Calendar mutations. Only
    title and colour are patched; times, reminders and event identity are retained.
    """
    with transaction.atomic():
        services.lock_user(user)
        session = WorkSession.objects.select_related('task').get(pk=session_id, user=user)
        state = copy.deepcopy(session.completion_presentation)
        completed = bool(session.task_id and session.task.is_completed)
        if session.status != 'published' or (not completed and not state):
            return
        if not completed and 'original_summary' not in state:
            session.completion_presentation = {'status': 'current', 'completed': False, 'error': ''}
            session.save(update_fields=['completion_presentation'])
            return
        try:
            reconciliation.check_idle(session)
            management.check_account(user, session)
            code, event = management.remote(user, session)
            management.managed_event(session, code, event)
            if completed and 'original_summary' not in state:
                state.update(original_summary=event.get('summary', ''), original_color=event.get('colorId'))
            title = PREFIX + state['original_summary'] if completed else state['original_summary']
            color = settings.COMPLETED_EVENT_COLOR_ID if completed else state.get('original_color')
            # Reopening must not erase an intervening manual title/colour edit.
            previous_title = state.get('applied_summary', state.get('original_summary'))
            previous_color = state.get('applied_color', state.get('original_color'))
            if event.get('summary', '') not in (previous_title, title, state.get('original_summary')) or event.get('colorId') not in (previous_color, color, state.get('original_color')):
                raise management.Conflict('Event title or colour changed in Google; review it before retrying completion sync.')
            if event.get('summary', '') != title or event.get('colorId') != color:
                # Save originals before a potentially uncertain provider response.
                state.update(applied_summary=title, applied_color=color, status='pending', error='')
                session.completion_presentation = state
                session.save(update_fields=['completion_presentation'])
                code, event = management.remote(user, session, 'PATCH',
                    payload={'summary': title, 'colorId': color}, etag=event['etag'])
                management.managed_event(session, code, event)
                if event.get('summary') != title or event.get('colorId') != color:
                    raise services.CalendarError('Google did not confirm the completion appearance.')
                session.management_version += 1
                session.changes.filter(status='proposed').update(status='rejected')
            state.update(status='current', completed=completed, applied_summary=title, applied_color=color, error='')
            session.event_payload.update(summary=title)
            if color is None:
                session.event_payload.pop('colorId', None)
            else:
                session.event_payload['colorId'] = color
            session.remote_snapshot = reconciliation.summary(event)
            session.remote_etag = event['etag']
            session.synced_at = timezone.now()
            session.completion_presentation = state
            session.save(update_fields=['completion_presentation', 'event_payload', 'remote_snapshot',
                'remote_etag', 'synced_at', 'management_version'])
        except APIException:
            # Never roll back reported progress or leak provider/token details.
            state.update(status='error', error='Completion appearance could not be synced. Check Calendar access or event edits, then retry session refresh.')
            session.completion_presentation = state
            session.save(update_fields=['completion_presentation'])

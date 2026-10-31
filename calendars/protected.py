"""Atomic protected-period edits with review of existing local Calendar commitments."""
import copy
import hashlib
import json

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError

from . import conflicts, services
from .models import ProtectedPeriod, SchedulingPreferences, WorkSession, SessionChange
from .scheduling import grey_details
from .serializers import ProtectedPeriodSerializer


class ImpactConflict(APIException):
    status_code = 409

    def __init__(self, detail):
        self.detail = detail


class ImpactControls(serializers.Serializer):
    conflict_resolution = serializers.ChoiceField(choices=['keep', 'review'], required=False)
    impact_token = serializers.CharField(max_length=64, required=False, allow_blank=True)
    keep_session_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=1000)


def impact(user, period):
    items = []
    sessions = WorkSession.objects.filter(user=user, status__in=['publishing', 'published'],
                                         finished_at__isnull=True, ends_at__gt=timezone.now()).order_by('pk')
    if period.kind == 'one_off':
        sessions = sessions.filter(starts_at__lt=period.ends_at, ends_at__gt=period.starts_at)
    for session in sessions:
        overlaps = conflicts.reviewed(grey_details(user, session.starts_at, session.ends_at, periods=[period]),
                                     session.starts_at, session.ends_at)
        if overlaps:
            items.append({'session_id': str(session.pk), 'status': session.status,
                          'starts_at': session.starts_at.isoformat(), 'ends_at': session.ends_at.isoformat(),
                          'management_version': session.management_version, 'synced_at': session.synced_at,
                          'task_title': session.task_snapshot.get('title', ''), 'grey_conflicts': overlaps,
                          'exception_kept': all(conflicts.has_grey_exception(session, item) for item in overlaps)})
    values = ProtectedPeriodSerializer(period).data
    profile = SchedulingPreferences.objects.get(user=user)
    digest = hashlib.sha256(json.dumps({'user_id': str(user.pk), 'period': values,
                                       'timezone': profile.timezone, 'sessions': items},
                                      sort_keys=True, default=str).encode()).hexdigest()
    return {'affected_sessions': items, 'impact_token': digest, 'review_required': bool(items)}


def write(user, data, *, period_id=None):
    if not isinstance(data, dict):
        raise ValidationError('Expected a JSON object.')
    controls = ImpactControls(data={k: v for k, v in data.items() if k in ImpactControls().fields})
    controls.is_valid(raise_exception=True)
    options = controls.validated_data
    with transaction.atomic():
        services.lock_user(user)
        from django.shortcuts import get_object_or_404
        original = get_object_or_404(ProtectedPeriod, pk=period_id, user=user) if period_id is not None else None
        if not SchedulingPreferences.objects.filter(user=user).exists():
            raise ValidationError('Set your scheduling timezone first.')
        if not original and ProtectedPeriod.objects.filter(user=user).count() >= 100:
            raise ValidationError('Keep no more than 100 protected periods; edit or delete an existing period.')
        fields = {k: v for k, v in data.items() if k not in ImpactControls().fields}
        serializer = ProtectedPeriodSerializer(original, data=fields, partial=original is not None)
        serializer.is_valid(raise_exception=True)
        candidate = copy.copy(original) if original else ProtectedPeriod(user=user)
        for key, value in serializer.validated_data.items():
            setattr(candidate, key, value)
        preview = impact(user, candidate)
        affected = preview['affected_sessions']
        ids = {item['session_id'] for item in affected}
        keep_ids = {str(pk) for pk in options.get('keep_session_ids', [])}
        if keep_ids - ids:
            raise ValidationError('Keep only sessions listed in the current impact review.')
        if affected:
            if (any(item['status'] == 'publishing' for item in affected)
                    or SessionChange.objects.filter(session_id__in=ids, status='applying').exists()):
                raise ImpactConflict({'error': 'calendar_write_unconfirmed',
                                      'message': 'Recover the affected Calendar write before changing this protected period.'})
            if not options.get('conflict_resolution') or options.get('impact_token') != preview['impact_token']:
                raise ImpactConflict({'error': 'protected_period_review_required',
                    'message': 'Review affected sessions, then resend with impact_token and conflict_resolution keep or review. No protected period was saved.',
                    **preview})
            if options['conflict_resolution'] == 'keep' and not keep_ids:
                keep_ids = ids
        period = serializer.save(user=user)
        for item in affected:
            session = WorkSession.objects.select_for_update().get(pk=item['session_id'], user=user)
            # Edits invalidate prior exceptions for this period; other exceptions are unchanged.
            session.grey_exceptions = [saved for saved in session.grey_exceptions
                                       if saved.get('conflict', {}).get('period_id') != period.pk]
            if item['session_id'] in keep_ids:
                overlaps = conflicts.reviewed(grey_details(user, session.starts_at, session.ends_at, periods=[period]),
                                             session.starts_at, session.ends_at)
                session.grey_exceptions.extend({'starts_at': session.starts_at.isoformat(), 'ends_at': session.ends_at.isoformat(),
                    'conflict': overlap, 'approved_at': timezone.now().isoformat(), 'source': 'protected_period_review'} for overlap in overlaps)
            session.sync_issues = [issue for issue in session.sync_issues if not issue.startswith('outside_availability:')]
            current = conflicts.reviewed(grey_details(user, session.starts_at, session.ends_at), session.starts_at, session.ends_at)
            if any(not conflicts.has_grey_exception(session, overlap) for overlap in current):
                session.sync_issues.append('outside_availability: protected-period change requires review or a new session time')
            if session.sync_issues:
                session.sync_status = 'review'
            elif session.sync_status == 'review':
                session.sync_status = 'current' if session.synced_at and not session.sync_error else 'unknown'
            session.management_version += 1
            session.save(update_fields=['grey_exceptions', 'sync_issues', 'sync_status', 'management_version'])
            session.changes.filter(status='proposed').update(status='rejected', last_error='Protected period changed; review a new proposal.')
        result = ProtectedPeriodSerializer(period).data
        result['impact_review'] = {'resolution': options.get('conflict_resolution'),
                                  'kept_session_ids': sorted(keep_ids),
                                  'review_session_ids': sorted(ids - keep_ids),
                                  'google_events_changed': False}
        return result

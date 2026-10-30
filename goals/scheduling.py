"""Confirmed ambition-specific calendar rules, separate from prose context."""
import hashlib
import json
from datetime import datetime

from rest_framework import serializers
from calendars.serializers import SchedulingPreferencesSerializer, WorkingHoursSerializer


class SchedulingRulesSerializer(serializers.Serializer):
    not_before = serializers.DateTimeField(allow_null=True, default=None)
    deadline = serializers.DateTimeField(allow_null=True, default=None)
    working_hours = WorkingHoursSerializer(many=True, allow_null=True, default=None, max_length=50)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError('Provide only not_before, deadline and working_hours.')
        for field in ('not_before', 'deadline'):
            value = data.get(field)
            if value is not None and (not isinstance(value, str) or not (value.endswith('Z') or '+' in value[10:] or '-' in value[10:])):
                raise serializers.ValidationError({field: 'Include an explicit timezone offset or Z.'})
        return super().to_internal_value(data)

    def validate_working_hours(self, value):
        if value is None:
            return None
        return SchedulingPreferencesSerializer().validate_working_hours(value)

    def validate(self, data):
        if data['not_before'] and data['deadline'] and data['deadline'] <= data['not_before']:
            raise serializers.ValidationError('deadline must follow not_before.')
        return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in data.items()}


def basis(context):
    return hashlib.sha256(json.dumps({'timing': context.get('timing', ''), 'constraints': context.get('constraints', [])},
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def needs_review(plan):
    context = plan.context
    has_text = bool(context.get('timing', '').strip() or context.get('constraints'))
    has_rules = 'scheduling_rules' in context or 'scheduling_rules_basis' in context
    return (has_text or has_rules) and context.get('scheduling_rules_basis') != basis(context)


def apply_windows(plan, profile, start, end, windows):
    """Intersect confirmed rules with global allowed windows; never expand them."""
    if needs_review(plan):
        raise serializers.ValidationError('Confirm scheduling_rules for the saved timing and constraints before scheduling.')
    rules = plan.context.get('scheduling_rules') or {}
    earliest = datetime.fromisoformat(rules['not_before']) if rules.get('not_before') else None
    deadline = datetime.fromisoformat(rules['deadline']) if rules.get('deadline') else None
    clipped = [(max(a, earliest) if earliest else a, min(b, deadline) if deadline else b) for a, b in windows]
    clipped = [(a, b) for a, b in clipped if a < b]
    hours = rules.get('working_hours')
    if hours is not None:
        from calendars.scheduling import permitted_windows
        extra = permitted_windows(profile.user, start, end, working_hours=hours)
        clipped = [(max(a, c), min(b, d)) for a, b in clipped for c, d in extra if max(a, c) < min(b, d)]
    return clipped

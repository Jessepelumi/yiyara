"""Task-scoped activity availability, separate from personal grey periods."""
from rest_framework import serializers
from goals.scheduling import SchedulingRulesSerializer


class EvidenceSerializer(serializers.Serializer):
    source_message_id = serializers.UUIDField()
    quote = serializers.CharField(max_length=5000)

    def validate(self, data):
        if not data.get('source_message_id') or not data.get('quote'):
            raise serializers.ValidationError('Evidence needs both source_message_id and an exact quote.')
        return data


class ActivityAvailabilitySerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['unrestricted', 'unknown', 'confirmed'], default='unrestricted')
    reason = serializers.CharField(max_length=2000, allow_blank=True, default='')
    rules = SchedulingRulesSerializer(required=False)
    evidence = EvidenceSerializer(many=True, default=list, max_length=10)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['Provide only status, reason, rules and evidence.']})
        return super().to_internal_value(data or {'status': 'unrestricted'})

    def validate(self, data):
        if 'status' not in data:
            raise serializers.ValidationError({'status': 'Provide an explicit availability status.'})
        data.setdefault('reason', '')
        data.setdefault('evidence', [])
        if data['status'] == 'unknown':
            if not data['reason'].strip():
                raise serializers.ValidationError('Explain which activity availability is missing.')
            if data.get('rules') or data['evidence']:
                raise serializers.ValidationError('Unknown availability cannot include confirmed rules or evidence.')
        elif data['status'] == 'confirmed':
            rules = data.get('rules', {})
            if not any(rules.get(key) is not None for key in ('working_hours', 'not_before', 'deadline')) or not data['evidence']:
                raise serializers.ValidationError('Confirmed availability needs explicit time rules and user-message evidence.')
        elif data.get('rules') or data['evidence']:
            raise serializers.ValidationError('Unrestricted activity cannot include time rules or evidence.')
        for item in data['evidence']:
            item['source_message_id'] = str(item['source_message_id'])
        return data


def normalize_availability(value, plan):
    serializer = ActivityAvailabilitySerializer(data=value)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    from conversations.models import Message
    for item in data['evidence']:
        message = Message.objects.filter(pk=item['source_message_id'], conversation__plan=plan, role='user').first()
        if not message or item['quote'] not in message.content:
            raise serializers.ValidationError('Activity availability must quote a user message in this plan.')
    return data


def apply_task_windows(task, profile, start, end, windows):
    availability = task.availability or {}
    status = availability.get('status', 'unrestricted')
    if status == 'unknown':
        raise serializers.ValidationError('Confirm this activity\'s availability before scheduling: ' + availability.get('reason', ''))
    if status == 'unrestricted':
        return windows
    # Revalidate saved evidence, including restored records, before relying on it.
    normalized = normalize_availability(availability, task.goal.plan)
    from goals.scheduling import apply_time_rules
    return apply_time_rules(normalized['rules'], profile, start, end, windows)

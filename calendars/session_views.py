from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response

from .models import WorkSession, SessionChange
from .serializers import AvailabilitySerializer
from .views import CalendarView
from . import sessions


class SlotRequestSerializer(AvailabilitySerializer):
    preferred_start = serializers.DateTimeField(required=False)
    conflict_policy = serializers.ChoiceField(choices=['ask', 'avoid'], default='ask')

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({'non_field_errors': ['Expected a JSON object.']})
        if 'preferred_start' in data:
            value = data['preferred_start']
            if not isinstance(value, str) or not (value.endswith('Z') or '+' in value[10:] or '-' in value[10:]):
                raise serializers.ValidationError({'preferred_start': 'Include a timezone offset or Z.'})
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        preferred = attrs.get('preferred_start')
        if preferred and (not attrs['time_min'] <= preferred < attrs['time_max'] or preferred.second or preferred.microsecond):
            raise serializers.ValidationError({'preferred_start': 'Choose a minute-aligned time inside the search range.'})
        return attrs


class OverlapApprovalSerializer(serializers.Serializer):
    allow_overlap = serializers.BooleanField(default=False)
    conflict_token = serializers.CharField(max_length=64, allow_blank=True, default='')


class ConflictReviewMixin:
    def get_requires_conflict_approval(self, obj):
        return bool(obj.conflicts) and not obj.overlap_approved

    def get_message(self, obj):
        if getattr(obj, 'kind', '') == 'cancel':
            return 'Review cancellation of this calendar session; task progress will remain unchanged.'
        if obj.conflicts:
            if obj.overlap_approved:
                return 'Overlap was explicitly approved. Existing events remain unchanged.'
            return 'A suitable time was found, but it is occupied. Find another slot or approve overlap; existing events will remain unchanged.'
        return 'No calendar conflict was found for this proposal.'


class SessionChangeSerializer(ConflictReviewMixin, serializers.ModelSerializer):
    reason = serializers.CharField(source='request_data.reason', read_only=True)
    requires_conflict_approval = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()

    class Meta:
        model = SessionChange
        fields = ['id', 'session_id', 'client_id', 'kind', 'status', 'base_version', 'reason', 'before_event',
                  'starts_at', 'ends_at', 'expires_at', 'approved_at', 'applied_at', 'lease_until', 'last_error', 'created_at',
                  'conflicts', 'conflict_token', 'overlap_approved', 'requires_conflict_approval', 'message']
        read_only_fields = fields


class SessionSerializer(ConflictReviewMixin, serializers.ModelSerializer):
    changes = SessionChangeSerializer(many=True, read_only=True)
    requires_conflict_approval = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()
    class Meta:
        model = WorkSession
        fields = ['id', 'task_id', 'status', 'starts_at', 'ends_at', 'expires_at',
                  'destination_calendar', 'google_event_id', 'task_snapshot', 'last_error',
                  'approved_at', 'published_at', 'management_version', 'cancelled_at', 'changes',
                  'conflicts', 'conflict_token', 'overlap_approved', 'overlap_history', 'requires_conflict_approval', 'message']
        read_only_fields = fields


class ProposeSessionView(CalendarView):
    def post(self, request, task_id):
        serializer = SlotRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = sessions.propose(request.user, task_id, serializer.validated_data['time_min'],
                                   serializer.validated_data['time_max'],
                                   preferred_start=serializer.validated_data.get('preferred_start'),
                                   conflict_policy=serializer.validated_data['conflict_policy'])
        return Response(SessionSerializer(session).data, status=201)


class SessionDetailView(CalendarView):
    def get(self, request, session_id):
        session = get_object_or_404(WorkSession, pk=session_id, user=request.user)
        return Response(SessionSerializer(session).data)


class SessionApproveView(CalendarView):
    def post(self, request, session_id):
        serializer = OverlapApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(SessionSerializer(sessions.approve(request.user, session_id, **serializer.validated_data)).data)


class SessionRejectView(CalendarView):
    def post(self, request, session_id):
        return Response(SessionSerializer(sessions.reject(request.user, session_id)).data)


class ChangeRequestMixin:
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['Provide only supported fields in a JSON object.']})
        return super().to_internal_value(data)


class RescheduleRequestSerializer(ChangeRequestMixin, SlotRequestSerializer):
    client_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=True, default='')


class CancellationRequestSerializer(ChangeRequestMixin, serializers.Serializer):
    client_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=True, default='')


class RescheduleSessionView(CalendarView):
    kind = 'reschedule'
    request_serializer = RescheduleRequestSerializer

    def post(self, request, session_id):
        from . import management

        serializer = self.request_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        change, replayed = management.propose(request.user, session_id, self.kind, serializer.validated_data)
        return Response({'change': SessionChangeSerializer(change).data, 'replayed': replayed}, status=200 if replayed else 201)


class CancelSessionView(RescheduleSessionView):
    kind = 'cancel'
    request_serializer = CancellationRequestSerializer


class SessionChangeDetailView(CalendarView):
    def get(self, request, change_id):
        return Response(SessionChangeSerializer(get_object_or_404(SessionChange, pk=change_id, user=request.user)).data)


class SessionChangeApproveView(CalendarView):
    def post(self, request, change_id):
        from . import management

        serializer = OverlapApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        change = management.approve(request.user, change_id, **serializer.validated_data)
        return Response({'change': SessionChangeSerializer(change).data, 'session': SessionSerializer(change.session).data},
                        status=409 if change.status == 'conflicted' else 200)


class SessionChangeRejectView(CalendarView):
    def post(self, request, change_id):
        from . import management

        return Response(SessionChangeSerializer(management.reject(request.user, change_id)).data)

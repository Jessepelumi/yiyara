from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response

from .models import WorkSession
from .serializers import AvailabilitySerializer
from .views import CalendarView
from . import sessions


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkSession
        fields = ['id', 'task_id', 'status', 'starts_at', 'ends_at', 'expires_at',
                  'destination_calendar', 'google_event_id', 'task_snapshot', 'last_error',
                  'approved_at', 'published_at']
        read_only_fields = fields


class ProposeSessionView(CalendarView):
    def post(self, request, task_id):
        serializer = AvailabilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = sessions.propose(request.user, task_id, serializer.validated_data['time_min'],
                                   serializer.validated_data['time_max'])
        return Response(SessionSerializer(session).data, status=201)


class SessionDetailView(CalendarView):
    def get(self, request, session_id):
        session = get_object_or_404(WorkSession, pk=session_id, user=request.user)
        return Response(SessionSerializer(session).data)


class SessionApproveView(CalendarView):
    def post(self, request, session_id):
        return Response(SessionSerializer(sessions.approve(request.user, session_id)).data)


class SessionRejectView(CalendarView):
    def post(self, request, session_id):
        return Response(SessionSerializer(sessions.reject(request.user, session_id)).data)

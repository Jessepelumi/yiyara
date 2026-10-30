from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework import serializers
from rest_framework.response import Response

from calendars.services import lock_user
from calendars.views import CalendarView
from calendars.models import WorkSession
from goals.models import Goal, Plan
from goals.services import create_plan_revision
from .models import Task
from .serializers import TaskWriteSerializer, TaskOutcomeSerializer


class TaskCreateView(CalendarView):
    def post(self, request):
        with transaction.atomic():
            lock_user(request.user)
            if not isinstance(request.data, dict):
                raise ValidationError('Expected a JSON object.')
            goal_id = serializers.UUIDField().run_validation(request.data.get('goal_id'))
            goal = get_object_or_404(Goal, pk=goal_id, user=request.user)
            plan = Plan.objects.select_for_update().get(pk=goal.plan_id)
            serializer = TaskWriteSerializer(data=request.data, context={'goal': goal})
            serializer.is_valid(raise_exception=True)
            task = serializer.save(goal=goal)
            plan.version += 1
            plan.save(update_fields=['version', 'updated_at'])
            create_plan_revision(plan, 'Created task manually')
        return Response(TaskWriteSerializer(task).data, status=201)


class TaskDetailView(CalendarView):
    def get(self, request, task_id):
        task = get_object_or_404(Task, pk=task_id, goal__user=request.user)
        return Response(TaskWriteSerializer(task).data)

    def patch(self, request, task_id):
        with transaction.atomic():
            lock_user(request.user)
            task = get_object_or_404(Task.objects.select_related('goal'), pk=task_id, goal__user=request.user)
            plan = Plan.objects.select_for_update().get(pk=task.goal.plan_id)
            from .guards import ensure_editable
            ensure_editable(task, request.data)
            serializer = TaskWriteSerializer(task, data=request.data, partial=True, context={'goal': task.goal})
            serializer.is_valid(raise_exception=True)
            completed = serializer.validated_data.pop('is_completed', None)
            serializer.save()
            if completed is not None:
                from .outcomes import record_completion_toggle
                record_completion_toggle(task, plan, completed)
            plan.version += 1
            plan.save(update_fields=['version', 'updated_at'])
            create_plan_revision(plan, 'Updated task details')
        return Response(serializer.data)


class TaskOutcomeView(CalendarView):
    def get(self, request, task_id):
        from .models import TaskOutcome
        # History also remains readable after a task is deleted by a revision restore.
        task = Task.objects.filter(pk=task_id, goal__user=request.user).first()
        history = TaskOutcome.objects.filter(task_id=task_id, plan__user=request.user)
        if task is None and not history.exists():
            from django.http import Http404
            raise Http404
        return Response(TaskOutcomeSerializer(history, many=True).data)

    def post(self, request, task_id):
        from .outcomes import OutcomeRequestSerializer, record_outcome

        serializer = OutcomeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task, plan, outcome, replayed = record_outcome(request.user, task_id, serializer.validated_data)
        return Response({'outcome': TaskOutcomeSerializer(outcome).data, 'task': TaskWriteSerializer(task).data,
                         'plan_version': plan.version, 'replayed': replayed}, status=200 if replayed else 201)

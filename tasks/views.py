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
from .serializers import TaskWriteSerializer


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
            if WorkSession.objects.filter(task=task, status__in=['publishing', 'published']).exists():
                if set(request.data) - {'is_completed'}:
                    raise ValidationError('Only completion may change after publishing starts; event editing is not supported yet.')
            serializer = TaskWriteSerializer(task, data=request.data, partial=True, context={'goal': task.goal})
            serializer.is_valid(raise_exception=True)
            serializer.save()
            plan.version += 1
            plan.save(update_fields=['version', 'updated_at'])
            create_plan_revision(plan, 'Updated task details')
        return Response(serializer.data)

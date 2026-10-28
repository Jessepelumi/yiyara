import json

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle

from .models import Plan, PlanQuestion
from .planning import (
    DirectContextSerializer, QuestionAnswerSerializer, answer_question,
    apply_context_update, validate_planning_state,
)
from .serializers import PlanSerializer
from .services import create_plan_revision


def check_contextual(plan, version):
    if plan.planning_mode != 'contextual':
        raise ValidationError('This endpoint requires a contextual plan.')
    if plan.version != version:
        return Response({'error': 'plan_version_conflict', 'message': 'Refresh the plan before changing context.'}, status=409)
    return None


class PlanContextView(APIView):
    def patch(self, request, plan_id):
        serializer = DirectContextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fields = dict(serializer.validated_data)
        version = fields.pop('plan_version')
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            plan = get_object_or_404(Plan.objects.select_for_update(), pk=plan_id, user=request.user)
            conflict = check_contextual(plan, version)
            if conflict is not None:
                return conflict
            from conversations.models import Conversation, Message

            conversation, _ = Conversation.objects.get_or_create(plan=plan, defaults={'user': request.user})
            source = Message.objects.create(conversation=conversation, role='user',
                                            content=json.dumps(fields, ensure_ascii=False), metadata={'source': 'context_edit'})
            apply_context_update(plan, fields, source)
            validate_planning_state(plan)
            plan.version += 1
            plan.context_version += 1
            plan.save(update_fields=['version', 'context_version', 'updated_at'])
            create_plan_revision(plan, 'User corrected planning context')
        return Response(PlanSerializer(plan).data)


class QuestionAnswerView(APIView):
    def post(self, request, plan_id, question_id):
        serializer = QuestionAnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            plan = get_object_or_404(Plan.objects.select_for_update(), pk=plan_id, user=request.user)
            question = get_object_or_404(PlanQuestion, pk=question_id, plan=plan)
            # Replaying the same answer is harmless even after its original version changed.
            if question.status == 'answered' and question.answer == data['answer']:
                return Response(PlanSerializer(plan).data)
            conflict = check_contextual(plan, data['plan_version'])
            if conflict is not None:
                return conflict
            from conversations.models import Conversation, Message

            conversation, _ = Conversation.objects.get_or_create(plan=plan, defaults={'user': request.user})
            source = Message.objects.create(conversation=conversation, role='user', content=data['answer'],
                                            metadata={'source': 'question_answer', 'question_id': str(question.pk)})
            answer_question(plan, question, data['answer'], source)
            plan.planning_state = 'clarifying'
            plan.waiting_reason = ''
            plan.version += 1
            plan.context_version += 1
            plan.save(update_fields=['planning_state', 'waiting_reason', 'version', 'context_version', 'updated_at'])
            create_plan_revision(plan, 'User answered a clarification question')
        return Response(PlanSerializer(plan).data)


class TaskPlanProposalView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'task_planning'

    def post(self, request, plan_id):
        from ai.providers.base import AIConfigurationError
        from .services import PlanVersionConflict
        from .serializers import PlanChangeSerializer
        from .task_planning import TaskPlanningError, TaskPlanningRequestSerializer, propose_tasks

        serializer = TaskPlanningRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            assistant, change, replayed = propose_tasks(request.user, plan_id, serializer.validated_data)
            return Response({
                'reply': assistant.content, 'message_id': assistant.pk,
                'change': PlanChangeSerializer(change).data if change else None,
                'replayed': replayed,
            }, status=200 if replayed else 201)
        except PlanVersionConflict as exc:
            return Response({'error': 'plan_version_conflict', 'message': str(exc)}, status=409)
        except AIConfigurationError:
            return Response({'error': 'ai_not_configured', 'message': 'Task planning AI is not configured.'}, status=503)
        except TaskPlanningError as exc:
            return Response({'error': 'task_planning_failed', 'message': str(exc)}, status=502)

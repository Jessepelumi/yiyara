import logging

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.providers.base import AIConfigurationError
from goals.models import Plan, PlanChange
from goals.serializers import PlanChangeSerializer, PlanSerializer
from goals.services import (
    InvalidPlanOperation,
    PlanVersionConflict,
    apply_plan_change,
    reject_plan_change,
)

from .models import Conversation, Message
from .serializers import MessageSerializer, PlanMessageRequestSerializer
from .services import PlanIterationError, handle_plan_message

logger = logging.getLogger(__name__)


class PlanMessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_plan(self, request, plan_id):
        return get_object_or_404(
            Plan.objects.prefetch_related("goals__tasks"),
            id=plan_id,
            user=request.user,
        )

    def get(self, request, plan_id):
        plan = self.get_plan(request, plan_id)
        conversation, _ = Conversation.objects.get_or_create(
            plan=plan,
            defaults={"user": request.user},
        )
        messages = conversation.messages.select_related("scope_goal").all()
        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request, plan_id):
        plan = self.get_plan(request, plan_id)
        serializer = PlanMessageRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        requested_version = data.get("plan_version")
        if requested_version and requested_version != plan.version:
            return Response(
                {
                    "error": "plan_version_conflict",
                    "message": "Board changed. Refresh before sending another message.",
                },
                status=status.HTTP_409_CONFLICT,
            )

        scope_goal = None
        scope_goal_id = data.get("scope_goal_id")
        if scope_goal_id:
            scope_goal = get_object_or_404(plan.goals, id=scope_goal_id)

        conversation, _ = Conversation.objects.get_or_create(
            plan=plan,
            defaults={"user": request.user},
        )

        client_id = data.get("client_id")
        existing = None
        if client_id:
            existing = conversation.messages.filter(
                client_id=client_id,
                role=Message.Role.USER,
            ).first()
            if existing:
                assistant = conversation.messages.filter(
                    role=Message.Role.ASSISTANT,
                    metadata__request_client_id=str(client_id),
                ).first()
                if existing.metadata.get("ai_status") != "failed" or assistant:
                    return Response(
                        {
                            "conversation_id": conversation.id,
                            "messages": MessageSerializer(
                                [
                                    message
                                    for message in (existing, assistant)
                                    if message
                                ],
                                many=True,
                            ).data,
                            "change": None,
                        }
                    )

        try:
            user_message, assistant_message, change = handle_plan_message(
                plan,
                conversation,
                data["content"],
                scope_goal=scope_goal,
                client_id=client_id,
                user_message=existing,
            )
            return Response(
                {
                    "conversation_id": conversation.id,
                    "messages": MessageSerializer(
                        [user_message, assistant_message], many=True
                    ).data,
                    "change": PlanChangeSerializer(change).data if change else None,
                },
                status=status.HTTP_201_CREATED,
            )
        except AIConfigurationError as exc:
            return Response(
                {"error": "ai_not_configured", "message": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except PlanIterationError as exc:
            return Response(
                {"error": "plan_iteration_failed", "message": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class ApplyPlanChangeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plan_id, change_id):
        change = get_object_or_404(
            PlanChange,
            id=change_id,
            plan_id=plan_id,
            plan__user=request.user,
        )
        try:
            plan, change = apply_plan_change(change, request.user)
            plan = Plan.objects.prefetch_related("goals__tasks").get(id=plan.id)
            return Response(
                {
                    "plan": PlanSerializer(plan).data,
                    "change": PlanChangeSerializer(change).data,
                }
            )
        except PlanVersionConflict as exc:
            return Response(
                {"error": "plan_version_conflict", "message": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except InvalidPlanOperation as exc:
            return Response(
                {"error": "invalid_plan_change", "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )


class RejectPlanChangeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plan_id, change_id):
        change = get_object_or_404(
            PlanChange,
            id=change_id,
            plan_id=plan_id,
            plan__user=request.user,
        )
        try:
            change = reject_plan_change(change, request.user)
            return Response(PlanChangeSerializer(change).data)
        except InvalidPlanOperation as exc:
            return Response(
                {"error": "invalid_plan_change", "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

from rest_framework.views import APIView
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Count
from rest_framework import status, permissions
from workflow import ai_engine
from .serializers import (
    DecomposeGoalRequestSerializer,
    GoalDecompositionSerializer,
    GoalSerializer,
    PlanChangeSerializer,
    PlanRevisionSerializer,
    PlanSerializer,
    PlanSummarySerializer,
    PreviewPlanSerializer,
)
from .models import Goal, Plan, PlanChange, PlanRevision
from .services import (
    InvalidPlanOperation,
    create_plan_revision,
    restore_plan_revision,
)
import logging

logger = logging.getLogger(__name__)


class PreviewDecomposeGoalView(APIView):
    """Generate a validated goal preview without creating database records."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "goal_preview"

    def post(self, request):
        request_serializer = DecomposeGoalRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        raw_input = request_serializer.validated_data["text"]

        try:
            workflow = ai_engine.YiyaraWorkflow()
            goal_data = workflow.decompose_goal(raw_input)
            preview = {
                "title": raw_input[:255],
                "raw_input": raw_input,
                "goals": goal_data,
            }
            response_serializer = PreviewPlanSerializer(preview)
            return Response(response_serializer.data, status=status.HTTP_200_OK)
        except ai_engine.GeminiConfigurationError as exc:
            return Response(
                {"error": "ai_not_configured", "message": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ai_engine.GoalDecompositionError as exc:
            logger.warning("Anonymous goal preview failed: %s", exc)
            return Response(
                {"error": "goal_decomposition_failed", "message": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception:
            logger.exception("Unexpected anonymous goal preview failure")
            return Response(
                {
                    "error": "goal_decomposition_failed",
                    "message": "Unexpected server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class DecomposeGoalView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        request_serializer = DecomposeGoalRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        raw_input = request_serializer.validated_data["text"]

        try:
            workflow = ai_engine.YiyaraWorkflow()
            plan = workflow.create_plan_from_ai(request.user, raw_input)
            serializer = PlanSerializer(plan)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except ai_engine.GeminiConfigurationError as exc:
            return Response(
                {"error": "ai_not_configured", "message": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ai_engine.GoalDecompositionError as exc:
            logger.warning(
                "Goal decomposition failed for user %s: %s", request.user.id, exc
            )
            return Response(
                {"error": "goal_decomposition_failed", "message": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception:
            logger.exception(
                "Unexpected goal decomposition failure for user %s", request.user.id
            )
            return Response(
                {
                    "error": "goal_decomposition_failed",
                    "message": "Unexpected server error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ImportPreviewPlanView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = PreviewPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = ai_engine.YiyaraWorkflow.persist_plan(
            request.user,
            data["raw_input"],
            data["goals"],
            title=data["title"],
        )
        return Response(PlanSerializer(plan).data, status=status.HTTP_201_CREATED)


class GoalListView(ListAPIView):
    """
    Returns a list of all goals and their nested tasks
    for the authenticated user.
    """

    serializer_class = GoalSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return (
            Goal.objects.filter(user=self.request.user)
            .select_related("plan")
            .prefetch_related("tasks")
            .order_by("-created_at")
        )

    def list(self, request, *args, **kwargs):
        try:
            return super().list(request, *args, **kwargs)
        except Exception:
            logger.exception("Error fetching goals for user %s", request.user.id)
            return Response(
                {"error": "Failed to retrieve goals."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class DeleteGoalView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        goal = get_object_or_404(Goal, pk=pk, user=request.user)
        
        try:
            with transaction.atomic():
                plan = Plan.objects.select_for_update().get(id=goal.plan_id)
                if plan.goals.count() <= 1:
                    return Response(
                        {
                            "error": "last_goal",
                            "message": "A drawing board must retain at least one goal.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                title = goal.title
                goal.delete()
                plan.version += 1
                plan.save(update_fields=["version", "updated_at"])
                create_plan_revision(plan, f'Deleted goal "{title}"')
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception:
            logger.exception("Error deleting goal %s", pk)
            return Response(
                {"error": "Failed to delete goal"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class PlanListView(ListAPIView):
    serializer_class = PlanSummarySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return (
            Plan.objects.filter(user=self.request.user)
            .annotate(
                goal_count=Count("goals", distinct=True),
                task_count=Count("goals__tasks", distinct=True),
            )
            .prefetch_related("goals")
            .order_by("-updated_at")
        )


class PlanDetailView(RetrieveAPIView):
    serializer_class = PlanSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_url_kwarg = "plan_id"

    def get_queryset(self):
        return (
            Plan.objects.filter(user=self.request.user)
            .prefetch_related("goals__tasks")
            .select_related("conversation")
        )


class PlanChangeListView(ListAPIView):
    serializer_class = PlanChangeSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PlanChange.objects.filter(
            plan_id=self.kwargs["plan_id"],
            plan__user=self.request.user,
        ).select_related("scope_goal")


class PlanRevisionListView(ListAPIView):
    serializer_class = PlanRevisionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PlanRevision.objects.filter(
            plan_id=self.kwargs["plan_id"],
            plan__user=self.request.user,
        )


class RestorePlanRevisionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, plan_id, version):
        revision = get_object_or_404(
            PlanRevision,
            plan_id=plan_id,
            plan__user=request.user,
            version=version,
        )
        try:
            plan = restore_plan_revision(revision, request.user)
            plan = Plan.objects.prefetch_related("goals__tasks").get(id=plan.id)
            return Response(PlanSerializer(plan).data)
        except InvalidPlanOperation as exc:
            return Response(
                {"error": "invalid_revision", "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

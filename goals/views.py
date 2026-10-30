from rest_framework.views import APIView
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models.deletion import RestrictedError
from django.db.models import Count
from rest_framework import status, permissions
from rest_framework.exceptions import ValidationError as ApiValidationError
from .serializers import (
    GoalSerializer,
    PlanChangeSerializer,
    PlanRevisionSerializer,
    PlanSerializer,
    PlanSummarySerializer,
    AmbitionSerializer,
)
from .models import Goal, Plan, PlanChange, PlanRevision
from .services import (
    InvalidPlanOperation,
    create_plan_revision,
    restore_plan_revision,
)
import logging

logger = logging.getLogger(__name__)


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
                from django.contrib.auth import get_user_model

                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                plan = Plan.objects.select_for_update().get(id=goal.plan_id)
                if plan.goals.count() <= 1:
                    return Response(
                        {
                            "error": "last_goal",
                            "message": "A drawing board must retain at least one goal.",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                from tasks.guards import ensure_goal_editable
                ensure_goal_editable(goal)
                title = goal.title
                goal.delete()
                plan.version += 1
                plan.save(update_fields=["version", "updated_at"])
                create_plan_revision(plan, f'Deleted goal "{title}"')
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ApiValidationError:
            raise
        except RestrictedError:
            return Response(
                {'error': 'task_dependencies', 'message': 'Other tasks depend on this goal. Update their prerequisites first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Error deleting goal %s", pk)
            return Response(
                {"error": "Failed to delete goal"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class PlanListView(ListAPIView):
    serializer_class = PlanSummarySerializer
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .planning import create_ambition

        serializer = AmbitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = create_ambition(request.user, serializer.validated_data["text"],
                               serializer.validated_data.get("title"))
        return Response(PlanSerializer(plan).data, status=status.HTTP_201_CREATED)

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
            .prefetch_related("goals__tasks", "questions")
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

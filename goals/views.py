from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from django.shortcuts import get_object_or_404
from rest_framework import status, permissions
from workflow import ai_engine
from .serializers import (
    DecomposeGoalRequestSerializer,
    GoalDecompositionSerializer,
    GoalSerializer,
)
from .models import Goal
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
            workflow = ai_engine.ZimnaWorkflow()
            goal_data = workflow.decompose_goal(raw_input)
            response_serializer = GoalDecompositionSerializer(goal_data, many=True)
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
                {"error": "goal_decomposition_failed", "message": "Unexpected server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class DecomposeGoalView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        request_serializer = DecomposeGoalRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        raw_input = request_serializer.validated_data['text']

        try:
            workflow = ai_engine.ZimnaWorkflow()
            created_goals = workflow.create_goals_from_ai(request.user, raw_input)
            serializer = GoalSerializer(created_goals, many=True)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except ai_engine.GeminiConfigurationError as exc:
            return Response(
                {"error": "ai_not_configured", "message": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ai_engine.GoalDecompositionError as exc:
            logger.warning("Goal decomposition failed for user %s: %s", request.user.id, exc)
            return Response(
                {"error": "goal_decomposition_failed", "message": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY
            )
        except Exception:
            logger.exception("Unexpected goal decomposition failure for user %s", request.user.id)
            return Response(
                {"error": "goal_decomposition_failed", "message": "Unexpected server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class GoalListView(ListAPIView):
    """
    Returns a list of all goals and their nested tasks
    for the authenticated user.
    """

    serializer_class = GoalSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Goal.objects.filter(user=self.request.user).prefetch_related('tasks').order_by('-created_at')
    
    def list(self, request, *args, **kwargs):
        try:
            return super().list(request, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error fetching goals for user {request.user.id}: {str(e)}")
            return Response(
                {"error": "Failed to reterieve goals.", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class DeleteGoalView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        goal = get_object_or_404(Goal, pk=pk, user=request.user)
        
        try:
            goal.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            logger.error(f"Error deleting goal {pk}: {str(e)}")
            return Response(
                {"error": "Failed to delete goal"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    

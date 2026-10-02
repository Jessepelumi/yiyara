import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from ai.gateway import AITask, get_ai_gateway
from ai.providers.base import AIConfigurationError, AIUnavailableError
from ai.prompts.goal_decomposition_prompt import (
    DECOMPOSITION_SYSTEM_PROMPT,
    GOAL_DECOMPOSITION_SCHEMA,
)
from goals.models import Goal, Plan
from goals.serializers import GoalDecompositionSerializer
from goals.services import create_plan_revision
from conversations.models import Conversation, Message
from tasks.models import Task

logger = logging.getLogger(__name__)


class GoalDecompositionError(Exception):
    """Raised when the AI cannot produce a valid, persistable decomposition."""


def validate_decomposition_response(ai_response):
    if not isinstance(ai_response, list) or not ai_response:
        raise GoalDecompositionError("AI returned no goals")

    serializer = GoalDecompositionSerializer(
        data=ai_response,
        many=True,
        allow_empty=False,
        max_length=10,
    )
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class YiyaraWorkflow:
    def __init__(self, gateway=None):
        self.gateway = gateway or get_ai_gateway()

    def create_plan_from_ai(self, user, raw_input):
        """Decompose one ambition and persist one shared drawing board."""
        goal_data = self.decompose_goal(raw_input)
        return self.persist_plan(user, raw_input, goal_data)

    def create_goals_from_ai(self, user, raw_input):
        """Backward-compatible command entry point returning created goals."""
        plan = self.create_plan_from_ai(user, raw_input)
        return list(plan.goals.prefetch_related("tasks").order_by("created_at"))

    def decompose_goal(self, raw_input):
        """Return a validated decomposition without writing anything to the DB."""

        prompt = f"User Input: '{raw_input}'\nCurrent Date: {timezone.now().date()}"

        try:
            goal_data = self.gateway.generate_json(
                task=AITask.GOAL_DECOMPOSITION,
                prompt=prompt,
                system_prompt=DECOMPOSITION_SYSTEM_PROMPT,
                response_schema=GOAL_DECOMPOSITION_SCHEMA,
                schema_name="goal_decomposition",
                temperature=0.1,
                max_output_tokens=settings.AI_MAX_OUTPUT_TOKENS,
                timeout_seconds=settings.AI_REQUEST_TIMEOUT_SECONDS,
                validator=validate_decomposition_response,
            )
        except GoalDecompositionError:
            raise
        except AIConfigurationError:
            raise
        except AIUnavailableError as exc:
            logger.warning("All goal decomposition providers failed: %s", exc)
            raise GoalDecompositionError(
                "AI providers are temporarily unavailable"
            ) from exc
        except serializers.ValidationError as exc:
            raise GoalDecompositionError(
                "AI returned invalid goal or task data"
            ) from exc
        except Exception as exc:
            logger.exception("Goal decomposition provider failed")
            raise GoalDecompositionError("AI provider failed") from exc

        return goal_data

    @staticmethod
    def persist_plan(user, raw_input, goal_data_list, title=None):
        created_goals = []

        with transaction.atomic():
            plan = Plan.objects.create(
                user=user,
                title=(title or raw_input)[:255],
                raw_input=raw_input,
            )

            for item in goal_data_list:
                new_goal = Goal.objects.create(
                    plan=plan,
                    user=user,
                    title=item.get("title", "Untitled Goal"),
                    description=item.get("description", ""),
                    raw_input=raw_input,
                    due_date=item.get("due_date") if item.get("due_date") else None,
                )

                tasks_to_create = [
                    Task(
                        goal=new_goal,
                        title=t["title"],
                        description=t.get("description", ""),
                        due_date=t.get("due_date"),
                    )
                    for t in item.get("tasks", [])
                ]
                Task.objects.bulk_create(tasks_to_create)

                created_goals.append(new_goal)

            conversation = Conversation.objects.create(plan=plan, user=user)
            Message.objects.create(
                conversation=conversation,
                role="assistant",
                content=(
                    f"I've broken this ambition into {len(created_goals)} goals. "
                    "Select a goal or discuss the whole board."
                ),
            )
            create_plan_revision(plan, "Initial decomposition")

        return plan

import json
import logging

from django.utils import timezone
from django.db import transaction
from rest_framework import serializers

from goals.models import Goal # Goal model
from goals.serializers import GoalDecompositionSerializer
from tasks.models import Task # Task model
from ai.providers.gemini_provider import GeminiConfigurationError, GeminiProvider
from ai.prompts.goal_decomposition_prompt import DECOMPOSITION_SYSTEM_PROMPT
from conversations.models import Conversation, Message

logger = logging.getLogger(__name__)


class GoalDecompositionError(Exception):
    """Raised when the AI cannot produce a valid, persistable decomposition."""


class ZimnaWorkflow:
    def __init__(self, api_key=None, provider=None):
        self.provider = provider or GeminiProvider(api_key=api_key)

    def create_goals_from_ai(self, user, raw_input):
        """
        The main entry point. Orchestrates AI decomposition and DB persistence.
        """

        # Prepare the dynamic prompt
        full_prompt = f"{DECOMPOSITION_SYSTEM_PROMPT}\n\nUser Input: '{raw_input}'\nCurrent Date: {timezone.now().date()}"

        try:
            ai_json_str = self.provider.generate_structured_response(full_prompt)
            ai_response = json.loads(ai_json_str)

            if not isinstance(ai_response, list) or not ai_response:
                raise GoalDecompositionError("AI returned no goals")

            serializer = GoalDecompositionSerializer(
                data=ai_response,
                many=True,
                allow_empty=False,
                max_length=10,
            )
            serializer.is_valid(raise_exception=True)
        except GoalDecompositionError:
            raise
        except json.JSONDecodeError as exc:
            raise GoalDecompositionError("AI returned invalid JSON") from exc
        except serializers.ValidationError as exc:
            raise GoalDecompositionError("AI returned invalid goal or task data") from exc
        except Exception as exc:
            logger.exception("Goal decomposition provider failed")
            raise GoalDecompositionError("AI provider failed") from exc

        return self._persist_to_db(user, raw_input, serializer.validated_data)


    def _persist_to_db(self, user, raw_input, goal_data_list):
        created_goals = []
        
        with transaction.atomic():
            for item in goal_data_list:
                # Create the Goal
                new_goal = Goal.objects.create(
                    user=user,
                    title=item.get('title', 'Untitled Goal'),
                    description=item.get('description', ''),
                    # raw_input helps us track what started this goal
                    raw_input=raw_input, 
                    due_date=item.get('due_date') if item.get('due_date') else None
                )

                # Create associated Tasks
                tasks_to_create = [
                    Task(
                        goal=new_goal,
                        title=t['title'],
                        description=t.get('description', ''),
                        due_date=t.get('due_date'),
                    ) for t in item.get('tasks', [])
                ]
                Task.objects.bulk_create(tasks_to_create)

                created_goals.append(new_goal)

            # Associate the conversation with the FIRST goal created for the chat context
            if created_goals:
                primary_goal = created_goals[0]
                conversation, _ = Conversation.objects.get_or_create(goal=primary_goal, user=user)
                Message.objects.create(
                    conversation=conversation,
                    role='assistant',
                    content=f"I've successfully broken down '{primary_goal.title}' into actionable steps! How would you like to start?"
                )

        return created_goals

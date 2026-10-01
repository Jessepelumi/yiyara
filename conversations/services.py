import json
import logging

from django.db import transaction

from ai.prompts.plan_iteration_prompt import PLAN_ITERATION_SYSTEM_PROMPT
from ai.providers.gemini_provider import GeminiProvider
from goals.services import (
    InvalidPlanOperation,
    create_plan_change,
    serialize_plan_snapshot,
)

from .models import Message

logger = logging.getLogger(__name__)


class PlanIterationError(RuntimeError):
    pass


def _recent_history(conversation, limit=10):
    messages = conversation.messages.order_by("-created_at")[:limit]
    return [
        {"role": message.role, "content": message.content}
        for message in reversed(messages)
    ]


def _build_iteration_prompt(plan, conversation, raw_text, scope_goal):
    board = serialize_plan_snapshot(plan)
    scope = (
        {
            "goal_id": str(scope_goal.id),
            "goal_title": scope_goal.title,
        }
        if scope_goal
        else {"goal_id": None, "goal_title": "Whole board"}
    )
    return (
        f"{PLAN_ITERATION_SYSTEM_PROMPT}\n\n"
        f"Current board:\n{json.dumps(board)}\n\n"
        f"Current scope:\n{json.dumps(scope)}\n\n"
        f"Recent conversation:\n{json.dumps(_recent_history(conversation))}\n\n"
        f"User message:\n{raw_text}"
    )


def handle_plan_message(
    plan,
    conversation,
    raw_text,
    *,
    scope_goal=None,
    client_id=None,
    provider=None,
):
    provider = provider or GeminiProvider()
    prompt = _build_iteration_prompt(plan, conversation, raw_text, scope_goal)
    user_message = Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content=raw_text,
        scope_goal=scope_goal,
        client_id=client_id,
    )

    try:
        payload = json.loads(provider.generate_plan_response(prompt))
        if not isinstance(payload, dict):
            raise PlanIterationError("AI returned an invalid board response")
        reply = payload.get("reply")
        summary = payload.get("summary") or "Proposed board changes"
        operations = payload.get("changes", [])
        if not isinstance(reply, str) or not reply.strip():
            raise PlanIterationError("AI returned an empty reply")

        with transaction.atomic():
            change = create_plan_change(plan, scope_goal, summary, operations)
            metadata = {}
            if change:
                metadata["change"] = {
                    "id": str(change.id),
                    "status": change.status,
                    "summary": change.summary,
                    "operations": change.operations,
                    "base_version": change.base_version,
                }
            assistant_message = Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content=reply.strip(),
                scope_goal=scope_goal,
                metadata=metadata,
            )
    except (json.JSONDecodeError, InvalidPlanOperation) as exc:
        logger.warning("Invalid plan iteration response for plan %s: %s", plan.id, exc)
        raise PlanIterationError("AI returned invalid board changes") from exc
    except PlanIterationError:
        raise
    except Exception as exc:
        logger.exception("Plan iteration provider failed for plan %s", plan.id)
        raise PlanIterationError("AI could not update this board") from exc

    return user_message, assistant_message, change

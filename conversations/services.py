import json
import logging

from django.conf import settings
from django.db import transaction

from ai.gateway import AITask, get_ai_gateway
from ai.prompts.plan_iteration_prompt import (
    PLAN_ITERATION_SCHEMA,
    PLAN_ITERATION_SYSTEM_PROMPT,
)
from ai.providers.base import AIConfigurationError, AIUnavailableError
from goals.services import (
    InvalidPlanOperation,
    create_plan_change,
    normalize_plan_operations,
    serialize_plan_snapshot,
)

from .models import Message

logger = logging.getLogger(__name__)


class PlanIterationError(RuntimeError):
    pass


def _validate_iteration_response(plan, payload):
    if not isinstance(payload, dict):
        raise PlanIterationError("AI returned an invalid board response")

    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise PlanIterationError("AI returned an empty reply")

    summary = payload.get("summary") or "Proposed board changes"
    if not isinstance(summary, str) or len(summary) > 500:
        raise PlanIterationError("AI returned an invalid change summary")

    operations = normalize_plan_operations(plan, payload.get("changes", []))
    return {
        "reply": reply.strip(),
        "summary": summary,
        "changes": operations,
    }


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
    gateway=None,
    user_message=None,
):
    gateway = gateway or get_ai_gateway()
    prompt = _build_iteration_prompt(plan, conversation, raw_text, scope_goal)
    if user_message is None:
        user_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content=raw_text,
            scope_goal=scope_goal,
            client_id=client_id,
            metadata={"ai_status": "pending"},
        )
    else:
        user_message.metadata = {**user_message.metadata, "ai_status": "pending"}
        user_message.save(update_fields=["metadata"])

    try:
        payload = gateway.generate_json(
            task=AITask.PLAN_ITERATION,
            prompt=prompt,
            system_prompt=PLAN_ITERATION_SYSTEM_PROMPT,
            response_schema=PLAN_ITERATION_SCHEMA,
            schema_name="plan_iteration",
            temperature=0.1,
            max_output_tokens=settings.AI_MAX_OUTPUT_TOKENS,
            timeout_seconds=settings.AI_REQUEST_TIMEOUT_SECONDS,
            validator=lambda response: _validate_iteration_response(plan, response),
        )
        reply = payload["reply"]
        summary = payload["summary"]
        operations = payload["changes"]

        with transaction.atomic():
            change = create_plan_change(plan, scope_goal, summary, operations)
            metadata = {
                "request_client_id": str(client_id) if client_id else None,
            }
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
            user_message.metadata = {**user_message.metadata, "ai_status": "completed"}
            user_message.save(update_fields=["metadata"])
    except InvalidPlanOperation as exc:
        _mark_message_failed(user_message)
        logger.warning("Invalid plan iteration response for plan %s: %s", plan.id, exc)
        raise PlanIterationError("AI returned invalid board changes") from exc
    except (AIConfigurationError, PlanIterationError):
        _mark_message_failed(user_message)
        raise
    except AIUnavailableError as exc:
        _mark_message_failed(user_message)
        logger.warning(
            "All plan iteration providers failed for plan %s: %s",
            plan.id,
            exc,
        )
        raise PlanIterationError("AI providers are temporarily unavailable") from exc
    except Exception as exc:
        _mark_message_failed(user_message)
        logger.exception("Plan iteration provider failed for plan %s", plan.id)
        raise PlanIterationError("AI could not update this board") from exc

    return user_message, assistant_message, change


def _mark_message_failed(message):
    message.metadata = {**message.metadata, "ai_status": "failed"}
    message.save(update_fields=["metadata"])

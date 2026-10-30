import json
import logging

from django.conf import settings
from config.policies import render_prompt
from django.db import transaction
from django.contrib.auth import get_user_model
from goals.models import Plan
from .requests import claim, ensure_current, complete, fail, retryable

from ai.gateway import AITask, get_ai_gateway
from ai.prompts.plan_iteration_prompt import (
    PLAN_ITERATION_SCHEMA,
    PLAN_ITERATION_SYSTEM_PROMPT,
)
from ai.providers.base import AIConfigurationError, AIUnavailableError
from goals.services import (
    InvalidPlanOperation,
    PlanVersionConflict,
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


def _recent_history(conversation, limit=None):
    limit = settings.CONVERSATION_HISTORY_LIMIT if limit is None else limit
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
    if plan.planning_mode == "contextual":
        from .clarification import handle_clarification

        return handle_clarification(plan, conversation, raw_text, client_id=client_id,
                                    gateway=gateway, user_message=user_message)
    gateway = gateway or get_ai_gateway()
    prompt = _build_iteration_prompt(plan, conversation, raw_text, scope_goal)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=plan.user_id)
        current = Plan.objects.get(pk=plan.pk)
        if current.version != plan.version:
            raise PlanVersionConflict('Plan changed before processing this request.')
        existing = conversation.messages.filter(client_id=client_id, role=Message.Role.USER).first() if client_id else None
        if existing:
            if existing.content != raw_text or not retryable(existing):
                raise PlanVersionConflict('This request is already processing or belongs to different content.')
            user_message = existing
        if user_message is None:
            user_message = Message.objects.create(conversation=conversation, role=Message.Role.USER,
                content=raw_text, scope_goal=scope_goal, client_id=client_id)
        claim(user_message)

    try:
        payload = gateway.generate_json(
            task=AITask.PLAN_ITERATION,
            prompt=prompt,
            system_prompt=render_prompt(PLAN_ITERATION_SYSTEM_PROMPT),
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
            get_user_model().objects.select_for_update().get(pk=plan.user_id)
            fresh = Plan.objects.get(pk=plan.pk)
            if fresh.version != plan.version:
                    raise PlanVersionConflict('Plan changed while AI responded.')
            ensure_current(user_message)
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
            complete(user_message)
    except InvalidPlanOperation as exc:
        _mark_message_failed(user_message)
        logger.warning("Invalid plan iteration response for plan %s: %s", plan.id, exc)
        raise PlanIterationError("AI returned invalid board changes") from exc
    except (AIConfigurationError, PlanIterationError, PlanVersionConflict):
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
    fail(message)

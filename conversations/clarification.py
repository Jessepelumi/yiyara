from config.policies import render_prompt
import json
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from ai.gateway import AITask, get_ai_gateway
from ai.prompts.clarification_prompt import CLARIFICATION_SCHEMA, CLARIFICATION_SYSTEM_PROMPT
from ai.providers.base import AIConfigurationError, AIUnavailableError
from goals.models import Plan
from goals.planning import CONTEXT_ACTIONS
from goals.services import (
    InvalidPlanOperation, PlanVersionConflict, create_plan_change,
    normalize_plan_operations, serialize_plan_snapshot,
)
from .models import Message
from .requests import retryable, claim, ensure_current, complete


def validate_response(plan, source, payload):
    from .services import PlanIterationError

    if not isinstance(payload, dict) or not isinstance(payload.get('reply'), str) or not payload['reply'].strip():
        raise PlanIterationError('AI returned an empty clarification response.')
    summary = payload.get('summary') or 'Proposed clarification updates'
    if not isinstance(summary, str) or len(summary) > 500:
        raise PlanIterationError('AI returned an invalid summary.')
    changes = payload.get('changes', [])
    if not isinstance(changes, list) or len(changes) > settings.PLAN_MAX_CLARIFICATION_CHANGES:
        raise PlanIterationError(f'Clarification proposals must contain at most {settings.PLAN_MAX_CLARIFICATION_CHANGES} changes.')
    if sum(isinstance(op, dict) and op.get('action') == 'ask_question' for op in changes) > settings.PLAN_MAX_QUESTIONS_PER_TURN:
        raise PlanIterationError(f'Ask at most {settings.PLAN_MAX_QUESTIONS_PER_TURN} focused questions per turn.')
    for operation in changes:
        if not isinstance(operation, dict) or operation.get('action') not in CONTEXT_ACTIONS:
            raise PlanIterationError('Only clarification changes are supported in this stage.')
        if operation.get('source_message_id') and operation['source_message_id'] != str(source.pk):
            raise PlanIterationError('New evidence must cite the current user message.')
        if operation.get('planning_state') == 'completed':
            raise PlanIterationError('Clarification cannot complete an ambition.')
    try:
        changes = normalize_plan_operations(plan, changes)
    except InvalidPlanOperation as exc:
        raise PlanIterationError('AI returned invalid clarification changes.') from exc
    return {'reply': payload['reply'].strip(), 'summary': summary, 'changes': changes}


def handle_clarification(plan, conversation, raw_text, *, client_id=None, gateway=None, user_message=None):
    from .services import PlanIterationError, _mark_message_failed

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=plan.user_id)
        current = Plan.objects.select_for_update().get(pk=plan.pk)
        if current.version != plan.version:
            raise PlanVersionConflict('Plan changed. Refresh before continuing.')
        if client_id:
            existing = conversation.messages.filter(client_id=client_id, role='user').first()
            if existing:
                if existing.content != raw_text:
                    raise PlanVersionConflict('Message client_id was already used with different content.')
                assistant = conversation.messages.filter(role='assistant', metadata__request_client_id=str(client_id)).first()
                if assistant:
                    return existing, assistant, None
                if not retryable(existing):
                    raise PlanVersionConflict('This message is already being processed.')
                user_message = existing
        if user_message is None:
            user_message = Message.objects.create(conversation=conversation, role='user', content=raw_text,
                                                  client_id=client_id, metadata={'ai_status': 'pending'})
        claim(user_message)
        snapshot = serialize_plan_snapshot(current)
    # The message and ambition survive failed model requests. No DB locks span AI work.
    try:
        from calendars.models import SchedulingPreferences

        profile = SchedulingPreferences.objects.filter(user_id=plan.user_id).first()
        zone = ZoneInfo(profile.timezone) if profile else ZoneInfo('UTC')
        history = list(conversation.messages.order_by('-created_at')[:settings.CONVERSATION_HISTORY_LIMIT])
        prompt = json.dumps({
            'current_local_time': timezone.now().astimezone(zone).isoformat(),
            'timezone': str(zone), 'timezone_is_fallback': profile is None,
            'scheduling_preferences': {
                'timezone': profile.timezone, 'working_hours': profile.working_hours,
                'preferred_session_minutes': profile.preferred_session_minutes,
            } if profile else None,
            'persistent_plan': snapshot,
            'recent_messages': [{'id': str(m.pk), 'role': m.role, 'content': m.content} for m in reversed(history)],
            'current_user_message_id': str(user_message.pk), 'current_user_message': raw_text,
        }, ensure_ascii=False)
        gateway = gateway or get_ai_gateway()
        payload = gateway.generate_json(
            task=AITask.PLAN_ITERATION, prompt=prompt, system_prompt=render_prompt(CLARIFICATION_SYSTEM_PROMPT),
            response_schema=CLARIFICATION_SCHEMA, schema_name='ambition_clarification', temperature=0.1,
            max_output_tokens=settings.AI_MAX_OUTPUT_TOKENS, timeout_seconds=settings.AI_REQUEST_TIMEOUT_SECONDS,
            validator=lambda data: validate_response(current, user_message, data),
        )
        # Validate even test/custom gateways that do not invoke the supplied validator.
        payload = validate_response(current, user_message, payload)
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=plan.user_id)
            fresh = Plan.objects.select_for_update().get(pk=plan.pk)
            if fresh.version != current.version:
                raise PlanVersionConflict('Context changed while Yiyara responded. Retry with the latest plan.')
            ensure_current(user_message)
            change = create_plan_change(fresh, None, payload['summary'], payload['changes'])
            metadata = {'request_client_id': str(client_id) if client_id else None}
            if change:
                metadata['change'] = {'id': str(change.pk), 'status': change.status, 'summary': change.summary,
                                      'operations': change.operations, 'base_version': change.base_version}
            assistant = Message.objects.create(conversation=conversation, role='assistant', content=payload['reply'], metadata=metadata)
            complete(user_message)
        return user_message, assistant, change
    except (AIConfigurationError, PlanIterationError, PlanVersionConflict):
        _mark_message_failed(user_message)
        raise
    except (AIUnavailableError, InvalidPlanOperation) as exc:
        _mark_message_failed(user_message)
        raise PlanIterationError('Clarification is temporarily unavailable. Your message is saved; retry it.') from exc
    except Exception as exc:
        _mark_message_failed(user_message)
        raise PlanIterationError('Clarification failed. Your message is saved; retry it.') from exc

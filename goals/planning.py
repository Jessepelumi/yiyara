"""Persistent clarification state and reviewable, bounded context operations."""
import copy
from django.conf import settings
from .scheduling import SchedulingRulesSerializer, basis

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import Plan, PlanQuestion

CONTEXT_FIELDS = ("desired_outcome", "starting_point", "timing", "constraints", "assumptions")
QUESTION_FIELDS = ("desired_outcome", "starting_point", "timing", "constraints", "other")
CONTEXT_ACTIONS = {"update_context", "ask_question", "answer_question", "defer_question"}


class ContextUpdateSerializer(serializers.Serializer):
    desired_outcome = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    starting_point = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    timing = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    constraints = serializers.ListField(child=serializers.CharField(max_length=2000), max_length=30, required=False)
    assumptions = serializers.ListField(child=serializers.CharField(max_length=2000), max_length=30, required=False)
    scheduling_rules = SchedulingRulesSerializer(required=False)
    planning_state = serializers.ChoiceField(choices=["clarifying", "actionable", "waiting", "completed"], required=False)
    waiting_reason = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide at least one context or planning-state field.")
        return attrs


class QuestionAnswerSerializer(serializers.Serializer):
    answer = serializers.CharField(max_length=10000)
    plan_version = serializers.IntegerField(min_value=1)


class DirectContextSerializer(ContextUpdateSerializer):
    plan_version = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if not (set(attrs) - {"plan_version"}):
            raise serializers.ValidationError("Provide a context update.")
        return attrs


def _source(plan, value):
    from conversations.models import Message

    try:
        return Message.objects.get(pk=value, conversation__plan=plan, role=Message.Role.USER)
    except (Message.DoesNotExist, ValueError, TypeError, DjangoValidationError):
        raise serializers.ValidationError("Context evidence must reference a user message in this plan.") from None


def _question(plan, value):
    from django.core.exceptions import ValidationError

    try:
        return PlanQuestion.objects.get(pk=value, plan=plan)
    except (PlanQuestion.DoesNotExist, ValueError, TypeError, ValidationError):
        raise serializers.ValidationError("Question does not belong to this plan.") from None


def normalize_context_operation(plan, operation):
    action = operation.get("action")
    clean = {"action": action}
    if plan.planning_mode != "contextual":
        raise serializers.ValidationError("Clarification operations require a contextual plan.")
    if action == "update_context":
        serializer = ContextUpdateSerializer(data=operation)
        serializer.is_valid(raise_exception=True)
        clean.update(serializer.validated_data)
        confirmed = [key for key in ("desired_outcome", "starting_point", "timing", "constraints") if key in clean]
        if confirmed or "scheduling_rules" in clean:
            source = _source(plan, operation.get("source_message_id"))
            for key in confirmed:
                values = clean[key] if isinstance(clean[key], list) else [clean[key]]
                if any(value and value not in source.content for value in values):
                    raise serializers.ValidationError("Confirmed context must quote the cited user message exactly.")
            clean["source_message_id"] = str(source.pk)
    elif action == "ask_question":
        text = serializers.CharField(max_length=2000).run_validation(operation.get("text"))
        field = serializers.ChoiceField(choices=QUESTION_FIELDS).run_validation(operation.get("field"))
        required = serializers.BooleanField().run_validation(operation.get("required", True))
        clean.update(text=text, field=field, required=required)
    elif action == "answer_question":
        question = _question(plan, operation.get("question_id"))
        answer = serializers.CharField(max_length=10000).run_validation(operation.get("answer"))
        source = _source(plan, operation.get("source_message_id"))
        if answer not in source.content:
            raise serializers.ValidationError("Question answers must quote the cited user message exactly.")
        clean.update(question_id=str(question.pk), answer=answer, source_message_id=str(source.pk))
    elif action == "defer_question":
        question = _question(plan, operation.get("question_id"))
        reason = serializers.CharField(max_length=2000).run_validation(operation.get("reason"))
        clean.update(question_id=str(question.pk), reason=reason)
    else:
        raise serializers.ValidationError("Unsupported clarification operation.")
    return clean


def apply_context_update(plan, fields, source=None):
    context = copy.deepcopy(plan.context)
    provenance = copy.deepcopy(plan.context_provenance)
    for field in CONTEXT_FIELDS:
        if field not in fields:
            continue
        context[field] = fields[field]
        old_sources = provenance.get(field, {}).get("source_message_ids", [])
        source_ids = list(dict.fromkeys([*old_sources, *([str(source.pk)] if source else [])]))
        provenance[field] = {
            "kind": "assumption" if field == "assumptions" else "user_statement",
            "source_message_id": str(source.pk) if source and field != "assumptions" else None,
            "source_message_ids": source_ids if field != "assumptions" else [],
        }
    if 'scheduling_rules' in fields:
        context['scheduling_rules'] = fields['scheduling_rules']
        context['scheduling_rules_basis'] = basis(context)
        provenance['scheduling_rules'] = {'kind': 'user_approved_rules', 'source_message_id': str(source.pk) if source else None}
    plan.context = context
    plan.context_provenance = provenance
    for field in ("planning_state", "waiting_reason"):
        if field in fields:
            setattr(plan, field, fields[field])
    if plan.planning_state != "waiting":
        plan.waiting_reason = ""
    plan.save(update_fields=["context", "context_provenance", "planning_state", "waiting_reason", "updated_at"])


def answer_question(plan, question, answer, source):
    old_answer = question.answer
    question.status = "answered"
    question.answer = answer
    question.source_message = source
    question.deferred_reason = ""
    question.save(update_fields=["status", "answer", "source_message", "deferred_reason"])
    if question.field != "other":
        value = answer
        if question.field == "constraints":
            value = [v for v in plan.context.get("constraints", []) if v != old_answer]
            if answer not in value:
                value.append(answer)
            if len(value) > 30:
                raise serializers.ValidationError("Keep no more than 30 constraints.")
        else:
            previous = plan.context.get(question.field, "")
            if old_answer and old_answer in previous:
                value = previous.replace(old_answer, answer, 1)
            elif previous and answer not in previous:
                value = previous + "\n" + answer
            else:
                value = previous or answer
            validator = ContextUpdateSerializer(data={question.field: value})
            validator.is_valid(raise_exception=True)
        apply_context_update(plan, {question.field: value}, source)


def apply_context_operation(plan, operation):
    action = operation["action"]
    if action == "update_context":
        source = _source(plan, operation["source_message_id"]) if operation.get("source_message_id") else None
        fields = dict(operation)
        # Model-proposed additions preserve earlier confirmed information. Explicit
        # user context PATCH remains the correction/replacement interface.
        for key in CONTEXT_FIELDS:
            if key not in fields:
                continue
            previous = plan.context.get(key)
            if isinstance(fields[key], list):
                fields[key] = list(dict.fromkeys([*(previous or []), *fields[key]]))
            elif previous and fields[key] and fields[key] not in previous:
                fields[key] = previous + "\n" + fields[key]
            elif previous:
                fields[key] = previous
        validated = ContextUpdateSerializer(data=fields)
        validated.is_valid(raise_exception=True)
        apply_context_update(plan, validated.validated_data, source)
    elif action == "ask_question":
        if plan.questions.filter(text=operation["text"], status="open").exists():
            return
        if plan.questions.count() >= settings.PLAN_MAX_QUESTIONS or plan.questions.filter(status="open").count() >= settings.PLAN_MAX_OPEN_QUESTIONS:
            raise serializers.ValidationError("Question limit reached; resolve existing questions first.")
        PlanQuestion.objects.create(plan=plan, text=operation["text"], field=operation["field"], required=operation["required"])
        if operation["required"] and plan.planning_state in ("actionable", "completed"):
            plan.planning_state = "clarifying"
            plan.save(update_fields=["planning_state"])
    elif action == "answer_question":
        answer_question(plan, _question(plan, operation["question_id"]), operation["answer"],
                        _source(plan, operation["source_message_id"]))
    elif action == "defer_question":
        question = _question(plan, operation["question_id"])
        question.status = "deferred"
        question.deferred_reason = operation["reason"]
        question.save(update_fields=["status", "deferred_reason"])


def validate_planning_state(plan):
    if plan.planning_state == "waiting" and not plan.waiting_reason.strip():
        raise serializers.ValidationError("Waiting requires a reason describing the missing information or external outcome.")
    if plan.planning_state in ("actionable", "completed") and plan.questions.filter(required=True).exclude(status="answered").exists():
        raise serializers.ValidationError("Resolve required questions before marking the plan actionable or completed.")


def create_ambition(user, text, title=None):
    from conversations.models import Conversation, Message
    from .services import create_plan_revision

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        plan = Plan.objects.create(user=user, raw_input=text, title=title or text[:255],
                                   planning_mode="contextual", planning_state="clarifying", context={
                                       "desired_outcome": text, "starting_point": "", "timing": "",
                                       "constraints": [], "assumptions": [],
                                   })
        conversation = Conversation.objects.create(plan=plan, user=user)
        source = Message.objects.create(conversation=conversation, role="user", content=text,
                                        metadata={"source": "ambition"})
        plan.context_provenance = {"desired_outcome": {
            "kind": "user_statement", "source_message_id": str(source.pk), "source_message_ids": [str(source.pk)]}}
        plan.save(update_fields=["context_provenance"])
        create_plan_revision(plan, "Saved ambition for clarification")
        return plan

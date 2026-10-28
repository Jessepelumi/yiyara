"""Bounded task proposals from approved context, using the existing change review API."""
import json
import re
import uuid
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from ai.gateway import AITask, get_ai_gateway
from ai.prompts.task_planning_prompt import TASK_PLANNING_SCHEMA, TASK_PLANNING_SYSTEM_PROMPT
from ai.providers.base import AIConfigurationError
from tasks.dependencies import plan_graph, validate_graph
from tasks.models import Task, TaskDependency
from .models import Goal, Plan


class TaskPlanningError(RuntimeError):
    pass


class TaskPlanningRequestSerializer(serializers.Serializer):
    plan_version = serializers.IntegerField(min_value=1)
    client_id = serializers.UUIDField()
    focus = serializers.CharField(max_length=3000, required=False, allow_blank=True, default='')


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError('Unexpected fields in task plan.')
        return super().to_internal_value(data)


class ProposedTaskSerializer(StrictSerializer):
    key = serializers.RegexField(r'^[a-z][a-z0-9_]{0,39}$')
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(max_length=5000)
    completion_criteria = serializers.CharField(max_length=3000)
    estimated_minutes = serializers.IntegerField(min_value=5, max_value=480, allow_null=True)
    readiness = serializers.ChoiceField(choices=['ready', 'draft', 'blocked'])
    blocker_reason = serializers.CharField(max_length=2000, allow_blank=True, default='')
    depends_on = serializers.ListField(child=serializers.CharField(max_length=64), max_length=50, default=list)

    def validate(self, attrs):
        if attrs['readiness'] == 'ready' and (not attrs['estimated_minutes'] or attrs['blocker_reason']):
            raise serializers.ValidationError('Ready tasks need effort and cannot have an external blocker.')
        if attrs['readiness'] == 'blocked' and not attrs['blocker_reason']:
            raise serializers.ValidationError('Blocked tasks require an explanation of what is missing.')
        if len(set(attrs['depends_on'])) != len(attrs['depends_on']):
            raise serializers.ValidationError('Duplicate prerequisites are not allowed.')
        return attrs


class ProposedGoalSerializer(StrictSerializer):
    goal_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    title = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True, default='')
    tasks = ProposedTaskSerializer(many=True, allow_empty=False, max_length=10)

    def validate(self, attrs):
        if not attrs['goal_id'] and not attrs['title']:
            raise serializers.ValidationError('New goals require a title.')
        return attrs


def ensure_plannable(plan):
    if plan.planning_mode != 'contextual' or plan.status != 'active' or plan.planning_state == 'completed':
        raise serializers.ValidationError('Task planning requires an active contextual ambition.')
    if not plan.context.get('desired_outcome', '').strip() or not plan.context.get('starting_point', '').strip():
        raise serializers.ValidationError('Clarify the desired outcome and starting point first.')
    if plan.questions.filter(required=True, status='open').exists():
        raise serializers.ValidationError('Answer or explicitly defer required questions before task planning.')
    if plan.questions.filter(required=True, status='deferred').exists() and plan.planning_state != 'waiting':
        raise serializers.ValidationError('Set a waiting reason for deferred required information first.')


def normalize_batch(plan, operation):
    ensure_plannable(plan)
    if operation.get('context_version') != plan.context_version:
        raise serializers.ValidationError('Planning context changed; request a new task proposal.')
    serializer = ProposedGoalSerializer(data=operation.get('goals'), many=True, allow_empty=False, max_length=3)
    serializer.is_valid(raise_exception=True)
    goals = serializer.validated_data
    if sum(len(goal['tasks']) for goal in goals) > 10:
        raise serializers.ValidationError('Propose at most ten new tasks.')
    graph = plan_graph(plan)
    keys = [task['key'] for goal in goals for task in goal['tasks']]
    if len(keys) != len(set(keys)):
        raise serializers.ValidationError('Task keys must be unique across the proposal.')
    graph.update({'new:' + key: set() for key in keys})
    duplicates = set(Task.objects.filter(goal__plan=plan, is_completed=False).values_list('title', 'completion_criteria'))
    duplicates = {(title.casefold(), criteria.casefold()) for title, criteria in duplicates}
    existing_titles = set(plan.goals.values_list('title', flat=True))
    existing_titles = {title.casefold() for title in existing_titles}
    seen_goals = set()
    known_text = json.dumps({key: value for key, value in plan.context.items() if key != 'assumptions'}, ensure_ascii=False) + plan.raw_input
    known_text += '\n'.join(plan.questions.filter(status='answered').values_list('answer', flat=True))
    for goal in goals:
        if goal['goal_id']:
            existing = plan.goals.filter(pk=goal['goal_id'], is_completed=False).first()
            if not existing:
                raise serializers.ValidationError('Existing goal must be unfinished and belong to this plan.')
            if ((goal['title'] and goal['title'] != existing.title)
                    or (goal['description'] and goal['description'] != existing.description)):
                raise serializers.ValidationError('Task generation cannot rename or rewrite an existing goal.')
            goal['goal_id'] = str(existing.pk)
            identity = goal['goal_id']
        else:
            identity = goal['title'].casefold()
            if identity in existing_titles:
                raise serializers.ValidationError('Reuse the existing goal ID rather than duplicating its title.')
        if identity in seen_goals:
            raise serializers.ValidationError('Group tasks for the same goal together.')
        seen_goals.add(identity)
        for task in goal['tasks']:
            signature = (task['title'].casefold(), task['completion_criteria'].casefold())
            if signature in duplicates:
                raise serializers.ValidationError('This proposal duplicates unfinished work.')
            duplicates.add(signature)
            for reference in task['depends_on']:
                if reference in keys:
                    parent = 'new:' + reference
                else:
                    try:
                        parent = str(uuid.UUID(reference))
                    except (ValueError, AttributeError):
                        raise serializers.ValidationError('Unknown prerequisite reference.') from None
                    if parent not in graph:
                        raise serializers.ValidationError('Prerequisites must belong to this plan.')
                graph['new:' + task['key']].add(parent)
            for field in ('title', 'description', 'completion_criteria'):
                for url in re.findall(r'https?://[^\s<>"\)]+', task[field]):
                    if url.rstrip('.,;!') not in known_text:
                        raise serializers.ValidationError('Only use resource URLs already supplied by the user.')
    validate_graph(graph)
    return {'action': 'add_task_batch', 'context_version': plan.context_version, 'goals': goals}


def apply_batch(plan, operation):
    # Revalidate the graph and context inside the existing user/plan transaction.
    operation = normalize_batch(plan, operation)
    created = {}
    for goal_data in operation['goals']:
        goal = plan.goals.get(pk=goal_data['goal_id']) if goal_data['goal_id'] else Goal.objects.create(
            plan=plan, user=plan.user, title=goal_data['title'], description=goal_data['description'], raw_input=plan.raw_input,
        )
        for data in goal_data['tasks']:
            created[data['key']] = Task.objects.create(
                goal=goal, title=data['title'], description=data['description'], completion_criteria=data['completion_criteria'],
                estimated_minutes=data['estimated_minutes'], readiness=data['readiness'], blocker_reason=data['blocker_reason'],
                planning_context_version=plan.context_version,
            )
    for goal_data in operation['goals']:
        for data in goal_data['tasks']:
            TaskDependency.objects.bulk_create([
                TaskDependency(task=created[data['key']], prerequisite_id=(created[ref].pk if ref in created else ref))
                for ref in data['depends_on']
            ])
    if plan.questions.filter(required=True).exclude(status='answered').exists():
        plan.planning_state = 'waiting'
        plan.waiting_reason = plan.waiting_reason or 'Required information is still pending; only independent work can proceed.'
    elif plan.planning_state != 'waiting':
        from tasks.dependencies import scheduling_blockers

        if not any(not scheduling_blockers(task) for task in Task.objects.filter(goal__plan=plan, is_completed=False)):
            plan.planning_state = 'waiting'
            plan.waiting_reason = next((t.blocker_reason for t in created.values() if t.blocker_reason),
                                       'Complete prerequisites or clarify task details before scheduling.')
        else:
            plan.planning_state = 'actionable'
            plan.waiting_reason = ''
    plan.save(update_fields=['planning_state', 'waiting_reason'])


def validate_result(plan, payload):
    if not isinstance(payload, dict):
        raise serializers.ValidationError('Invalid task-planning response.')
    reply = serializers.CharField(max_length=10000).run_validation(payload.get('reply'))
    summary = serializers.CharField(max_length=500).run_validation(payload.get('summary'))
    goals = payload.get('goals')
    if not isinstance(goals, list):
        raise serializers.ValidationError('Task plan goals must be a list.')
    operation = normalize_batch(plan, {'goals': goals, 'context_version': plan.context_version}) if goals else None
    return {'reply': reply, 'summary': summary, 'goals': operation['goals'] if operation else []}


def propose_tasks(user, plan_id, data):
    from conversations.models import Conversation, Message
    from .services import PlanVersionConflict, create_plan_change, serialize_plan_snapshot

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        plan = get_object_or_404(Plan.objects.select_for_update(), pk=plan_id, user=user)
        conversation, _ = Conversation.objects.get_or_create(plan=plan, defaults={'user': user})
        existing = conversation.messages.filter(client_id=data['client_id'], role='user').first()
        if existing:
            if existing.metadata.get('source') != 'task_planning' or existing.metadata.get('focus') != data['focus']:
                raise PlanVersionConflict('This client_id belongs to a different request.')
            assistant = conversation.messages.filter(role='assistant', metadata__request_client_id=str(data['client_id'])).first()
            if assistant:
                change = plan.changes.filter(pk=assistant.metadata.get('change_id')).first()
                return assistant, change, True
            if existing.metadata.get('ai_status') != 'failed':
                raise PlanVersionConflict('Task planning is already in progress. Retry this client_id shortly.')
        if plan.version != data['plan_version']:
            raise PlanVersionConflict('Plan changed. Refresh before requesting tasks.')
        ensure_plannable(plan)
        message = existing or Message.objects.create(
            conversation=conversation, role='user', content=data['focus'] or 'Propose the next actionable tasks from saved context.',
            client_id=data['client_id'],
        )
        message.metadata = {'source': 'task_planning', 'focus': data['focus'], 'ai_status': 'pending'}
        message.save(update_fields=['metadata'])
        captured_version = plan.version
        snapshot = serialize_plan_snapshot(plan)
    try:
        from calendars.models import SchedulingPreferences

        profile = SchedulingPreferences.objects.filter(user=user).first()
        zone = ZoneInfo(profile.timezone) if profile else ZoneInfo('UTC')
        prompt = json.dumps({
            'current_local_time': timezone.now().astimezone(zone).isoformat(), 'timezone': str(zone),
            'timezone_is_fallback': profile is None, 'persistent_plan': snapshot, 'focus': data['focus'],
            'near_term_horizon_days': 7,
            'scheduling_preferences': {'working_hours': profile.working_hours,
                                       'preferred_session_minutes': profile.preferred_session_minutes} if profile else None,
        }, ensure_ascii=False)
        gateway = get_ai_gateway()
        payload = gateway.generate_json(
            task=AITask.TASK_PLANNING, prompt=prompt, system_prompt=TASK_PLANNING_SYSTEM_PROMPT,
            response_schema=TASK_PLANNING_SCHEMA, schema_name='next_actionable_tasks', temperature=0.1,
            max_output_tokens=settings.AI_MAX_OUTPUT_TOKENS, timeout_seconds=settings.AI_REQUEST_TIMEOUT_SECONDS,
            validator=lambda result: validate_result(plan, result),
        )
        payload = validate_result(plan, payload)
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)
            fresh = Plan.objects.select_for_update().get(pk=plan.pk)
            if fresh.version != captured_version:
                raise PlanVersionConflict('Plan changed while tasks were being proposed. Retry with the current version.')
            ensure_plannable(fresh)
            change = create_plan_change(fresh, None, payload['summary'], [{
                'action': 'add_task_batch', 'context_version': fresh.context_version, 'goals': payload['goals'],
            }] if payload['goals'] else [])
            assistant = Message.objects.create(conversation=conversation, role='assistant', content=payload['reply'], metadata={
                'source': 'task_planning', 'request_client_id': str(data['client_id']),
                'change_id': str(change.pk) if change else None,
            })
            message.metadata['ai_status'] = 'completed'
            message.save(update_fields=['metadata'])
        return assistant, change, False
    except (AIConfigurationError, PlanVersionConflict):
        message.metadata['ai_status'] = 'failed'
        message.save(update_fields=['metadata'])
        raise
    except Exception as exc:
        message.metadata['ai_status'] = 'failed'
        message.save(update_fields=['metadata'])
        raise TaskPlanningError('Task planning failed. Your request is saved; retry the same client_id.') from exc

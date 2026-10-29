"""Reviewable edits and additions; task outcomes and external events stay immutable."""
import json
import re
import uuid

from django.db.models import Q
from rest_framework import serializers

from calendars.models import WorkSession
from tasks.dependencies import plan_graph, validate_graph
from tasks.models import Task, TaskDependency
from .task_planning import (
    StrictSerializer, ProposedGoalSerializer, ensure_plannable, normalize_batch,
    apply_batch, update_planning_state,
)

EDITABLE_FIELDS = ('title', 'description', 'completion_criteria', 'estimated_minutes', 'readiness', 'blocker_reason')


class RevisionFieldsSerializer(StrictSerializer):
    title = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(max_length=5000, required=False)
    completion_criteria = serializers.CharField(max_length=3000, required=False)
    estimated_minutes = serializers.IntegerField(min_value=5, max_value=480, allow_null=True, required=False)
    readiness = serializers.ChoiceField(choices=['ready', 'draft', 'blocked'], required=False)
    blocker_reason = serializers.CharField(max_length=2000, allow_blank=True, required=False)
    depends_on = serializers.ListField(child=serializers.CharField(max_length=64), max_length=50, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError('Supply at least one task revision.')
        refs = attrs.get('depends_on', [])
        if len(set(refs)) != len(refs):
            raise serializers.ValidationError('Duplicate prerequisites are not allowed.')
        return attrs


class RevisionSerializer(StrictSerializer):
    task_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000)
    changes = RevisionFieldsSerializer()
    before = serializers.DictField(read_only=True)


def protected_task_ids(plan):
    ids = list(Task.objects.filter(goal__plan=plan).values_list('pk', flat=True))
    strings = {str(pk) for pk in ids}
    sessions = WorkSession.objects.filter(
        user_id=plan.user_id, status__in=['publishing', 'published'],
    ).filter(Q(task_id__in=ids) | Q(task_snapshot__task_id__in=list(strings)))
    protected = set()
    for task_id, original_id in sessions.values_list('task_id', 'task_snapshot__task_id'):
        protected.update(value for value in (str(task_id), original_id) if value in strings)
    return protected


def normalize_replan(plan, operation):
    ensure_plannable(plan)
    if operation.get('context_version') != plan.context_version:
        raise serializers.ValidationError('Planning context changed; request a new replanning proposal.')
    updates = RevisionSerializer(data=operation.get('updates'), many=True, max_length=10)
    updates.is_valid(raise_exception=True)
    goals = ProposedGoalSerializer(data=operation.get('goals'), many=True, max_length=3)
    goals.is_valid(raise_exception=True)
    updates, goals = updates.validated_data, goals.validated_data
    if not updates and not goals:
        raise serializers.ValidationError('There is no work to revise or add.')
    if len(updates) + sum(len(goal['tasks']) for goal in goals) > 10:
        raise serializers.ValidationError('Revise or add at most ten tasks in one proposal.')
    task_ids = [str(item['task_id']) for item in updates]
    if len(set(task_ids)) != len(task_ids):
        raise serializers.ValidationError('Group changes for the same task together.')
    tasks = {str(task.pk): task for task in Task.objects.filter(goal__plan=plan).select_related('goal')}
    protected = protected_task_ids(plan)
    keys = [task['key'] for goal in goals for task in goal['tasks']]
    graph = plan_graph(plan)
    signatures = [(task.title, task.completion_criteria) for pk, task in tasks.items()
                  if pk not in task_ids and not task.is_completed]
    signature_keys = {(title.casefold(), criteria.casefold()) for title, criteria in signatures}
    # Current saved resources are allowed; model-created resources are not researched here.
    known = json.dumps({key: value for key, value in plan.context.items() if key != 'assumptions'}, ensure_ascii=False)
    known += plan.raw_input + '\n'.join(plan.questions.filter(status='answered').values_list('answer', flat=True))
    known += '\n'.join(task.description + '\n' + task.completion_criteria for task in tasks.values())
    allowed_urls = {url.rstrip('.,;!') for url in re.findall(r'https?://[^\s<>"\)]+', known)}
    result = []
    for item in updates:
        pk = str(item['task_id'])
        task = tasks.get(pk)
        if not task or task.is_completed or task.goal.is_completed:
            raise serializers.ValidationError('Revise only unfinished tasks in unfinished goals belonging to this plan.')
        if pk in protected:
            raise serializers.ValidationError('Tasks with publishing or published sessions cannot be revised in this stage.')
        if task.progress_status == 'skipped':
            raise serializers.ValidationError('Explicitly reopen skipped work before requesting revisions.')
        changes = dict(item['changes'])
        before = {field: getattr(task, field) for field in EDITABLE_FIELDS}
        before.update(depends_on=sorted(graph[pk]), remaining_minutes=task.remaining_minutes,
                      progress_status=task.progress_status, progress_version=task.progress_version,
                      planning_context_version=task.planning_context_version)
        after = {**before, **changes}
        if task.blocker_reason and after['blocker_reason'] != task.blocker_reason:
            raise serializers.ValidationError('Clear or correct reported external blockers explicitly before replanning.')
        if task.remaining_minutes is not None and after['estimated_minutes'] != task.estimated_minutes:
            raise serializers.ValidationError('Preserve the original estimate when remaining effort has been reported.')
        effort = task.remaining_minutes if task.remaining_minutes is not None else after['estimated_minutes']
        if after['readiness'] == 'ready' and (not effort or not after['completion_criteria'] or after['blocker_reason']):
            raise serializers.ValidationError('Ready tasks require effort and completion criteria and no external blocker.')
        if after['readiness'] == 'blocked' and not after['blocker_reason']:
            raise serializers.ValidationError('Blocked tasks require a blocker reason.')
        if 'depends_on' in changes:
            refs = []
            for ref in changes['depends_on']:
                if ref in keys:
                    refs.append('new:' + ref)
                else:
                    try:
                        parent = str(uuid.UUID(ref))
                    except ValueError:
                        raise serializers.ValidationError('Unknown prerequisite reference.') from None
                    if parent not in tasks:
                        raise serializers.ValidationError('Prerequisites must belong to this plan.')
                    refs.append(parent)
            graph[pk] = set(refs)
            if len(refs) != len(set(refs)):
                raise serializers.ValidationError('Duplicate prerequisites are not allowed.')
            changes['depends_on'] = [ref[4:] if ref.startswith('new:') else ref for ref in refs]
        meaningful = any(value != before[field] for field, value in changes.items() if field != 'depends_on')
        meaningful |= graph[pk] != set(before['depends_on'])
        # Explicit approval of readiness may reaffirm changed context even with identical task details.
        meaningful |= changes.get('readiness') == 'ready' and task.planning_context_version != plan.context_version
        if not meaningful:
            raise serializers.ValidationError('Replanning must explain an actual change; omit unchanged tasks.')
        signature = (after['title'], after['completion_criteria'])
        key = tuple(value.casefold() for value in signature)
        if key in signature_keys:
            raise serializers.ValidationError('A revision duplicates unfinished work.')
        signature_keys.add(key)
        signatures.append(signature)
        for field in ('title', 'description', 'completion_criteria'):
            for url in re.findall(r'https?://[^\s<>"\)]+', after[field]):
                if url.rstrip('.,;!') not in allowed_urls:
                    raise serializers.ValidationError('Only use saved resource URLs; no research tools are available.')
        result.append({'task_id': pk, 'reason': item['reason'], 'changes': changes, 'before': before})
    if goals:
        goals = normalize_batch(plan, {'goals': goals, 'context_version': plan.context_version},
                                existing_graph=graph, unfinished_signatures=signatures)['goals']
    else:
        validate_graph(graph)
    return {'action': 'replan_tasks', 'context_version': plan.context_version, 'updates': result, 'goals': goals}


def validate_replan_result(plan, payload):
    if not isinstance(payload, dict) or set(payload) != {'reply', 'summary', 'updates', 'goals'}:
        raise serializers.ValidationError('Replanning requires reply, summary, updates and goals only.')
    reply = serializers.CharField(max_length=10000).run_validation(payload['reply'])
    summary = serializers.CharField(max_length=500).run_validation(payload['summary'])
    if payload['updates'] == [] and payload['goals'] == []:
        return {'reply': reply, 'summary': summary, 'updates': [], 'goals': []}
    operation = normalize_replan(plan, {**payload, 'context_version': plan.context_version})
    return {'reply': reply, 'summary': summary, 'updates': operation['updates'], 'goals': operation['goals']}


def apply_replan(plan, operation):
    # Runs under the shared user lock and existing change-approval transaction.
    operation = normalize_replan(plan, operation)
    affected = []
    for item in operation['updates']:
        task = Task.objects.select_related('goal').get(pk=item['task_id'], goal__plan=plan)
        for field, value in item['changes'].items():
            if field != 'depends_on':
                setattr(task, field, value)
        if item['changes'].get('readiness') == 'ready':
            task.planning_context_version = plan.context_version
        task.save()
        if 'depends_on' in item['changes']:
            TaskDependency.objects.filter(task=task).delete()
        affected.append(task)
    created = apply_batch(plan, {'goals': operation['goals'], 'context_version': plan.context_version}) if operation['goals'] else {}
    for item in operation['updates']:
        if 'depends_on' in item['changes']:
            TaskDependency.objects.bulk_create([
                TaskDependency(task_id=item['task_id'], prerequisite_id=created[ref].pk if ref in created else ref)
                for ref in item['changes']['depends_on']
            ])
    WorkSession.objects.filter(user_id=plan.user_id, status='proposed',
                               task_snapshot__task_id__in=[item['task_id'] for item in operation['updates']]).update(status='rejected')
    update_planning_state(plan, [*affected, *created.values()])

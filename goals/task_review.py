"""Review user-added tasks using the normal proposed-change and approval lifecycle."""
from copy import deepcopy
from django.conf import settings
from rest_framework import serializers
from tasks.models import Task
from .task_planning import TaskPlanningRequestSerializer
from .replanning import validate_replan_result
from ai.prompts.replanning_prompt import REPLANNING_SCHEMA, REPLANNING_SYSTEM_PROMPT


class TaskReviewRequestSerializer(TaskPlanningRequestSerializer):
    task_ids = serializers.ListField(child=serializers.UUIDField(), default=list)

    def validate_task_ids(self, value):
        if len(value) != len(set(value)) or len(value) > settings.PLAN_MAX_TASKS_PER_PROPOSAL:
            raise serializers.ValidationError('Select unique task IDs within the configured per-proposal limit.')
        return value


def selected_ids(plan, requested):
    ids = [str(pk) for pk in requested] if requested else list(
        Task.objects.filter(goal__plan=plan).order_by('created_at', 'pk').values_list('pk', flat=True))
    ids = [str(pk) for pk in ids]
    if not ids or len(ids) > settings.PLAN_MAX_TASKS_PER_PROPOSAL:
        raise serializers.ValidationError('Select task_ids for one nonempty review batch within the configured proposal limit.')
    if Task.objects.filter(goal__plan=plan, pk__in=ids).count() != len(ids):
        raise serializers.ValidationError('Review only tasks belonging to this plan.')
    return ids


class AssessmentSerializer(serializers.Serializer):
    task_id = serializers.UUIDField()
    fit = serializers.ChoiceField(choices=['aligned', 'unclear', 'unrelated', 'duplicate', 'needs_clarification'])
    reason = serializers.CharField(max_length=2000)
    related_task_ids = serializers.ListField(child=serializers.UUIDField(), default=list, max_length=50)


def validate_review_result(plan, payload, ids):
    if not isinstance(payload, dict) or set(payload) != {'reply', 'summary', 'updates', 'goals', 'assessment'}:
        raise serializers.ValidationError('Review requires reply, summary, updates, goals and assessment only.')
    serializer = AssessmentSerializer(data=payload['assessment'], many=True)
    serializer.is_valid(raise_exception=True)
    assessment = serializer.validated_data
    seen = [str(item['task_id']) for item in assessment]
    if len(seen) != len(set(seen)) or set(seen) != set(ids):
        raise serializers.ValidationError('Assess every selected task exactly once.')
    all_ids = {str(pk) for pk in Task.objects.filter(goal__plan=plan).values_list('pk', flat=True)}
    for item in assessment:
        item['task_id'] = str(item['task_id'])
        item['related_task_ids'] = [str(pk) for pk in item['related_task_ids']]
        if (len(item['related_task_ids']) != len(set(item['related_task_ids']))
                or set(item['related_task_ids']) - all_ids or item['task_id'] in item['related_task_ids']):
            raise serializers.ValidationError('Related tasks must be distinct other tasks in this plan.')
    result = validate_replan_result(plan, {k: v for k, v in payload.items() if k != 'assessment'})
    if any(str(item['task_id']) not in ids for item in result['updates']):
        raise serializers.ValidationError('Review changes may edit only the selected tasks.')
    return {**result, 'assessment': assessment}


TASK_REVIEW_SYSTEM_PROMPT = REPLANNING_SYSTEM_PROMPT + '''
This request reviews a user's saved task list. Return assessment in addition to
reply, summary, updates and goals. Assess EVERY task in review_task_ids exactly once:
task_id, fit (aligned/unclear/unrelated/duplicate/needs_clarification), reason and
related_task_ids. Consider the entire plan when checking duplicates, prerequisites,
effort, context and scope, but propose updates ONLY to selected review_task_ids.
Do not silently replace a user's intent to fit the plan. An unrelated task may belong
to another ambition; explain and ask rather than deleting or rewriting its purpose.
Do not call a user-added task approved by AI or already edited. All suggestions are
reviewable. If no edits are useful return updates=[] and goals=[] with assessments.
This is not a Calendar operation. Respect protected tasks and recorded outcomes.
'''
TASK_REVIEW_SCHEMA = deepcopy(REPLANNING_SCHEMA)
TASK_REVIEW_SCHEMA['required'].append('assessment')
TASK_REVIEW_SCHEMA['properties']['assessment'] = {'type': 'array', 'items': {
    'type': 'object', 'required': ['task_id', 'fit', 'reason', 'related_task_ids'],
    'additionalProperties': False, 'properties': {
        'task_id': {'type': 'string'}, 'fit': {'type': 'string', 'enum': ['aligned', 'unclear', 'unrelated', 'duplicate', 'needs_clarification']},
        'reason': {'type': 'string'}, 'related_task_ids': {'type': 'array', 'items': {'type': 'string'}}}}}

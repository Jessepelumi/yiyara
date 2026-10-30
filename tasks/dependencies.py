"""Dependency integrity and the common scheduling gate for all task sources."""
from django.conf import settings
from config.policies import valid_effort
from rest_framework.exceptions import ValidationError

from .models import Task, TaskDependency


def validate_graph(graph):
    # Iterative topological removal avoids recursion limits on long-lived plans.
    remaining = {node: set(parents) for node, parents in graph.items()}
    children = {node: set() for node in remaining}
    for node, parents in remaining.items():
        for parent in parents:
            if parent not in remaining:
                raise ValidationError('Dependencies must belong to the same plan.')
            children[parent].add(node)
    queue = [node for node, parents in remaining.items() if not parents]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for child in children[node]:
            remaining[child].discard(node)
            if not remaining[child]:
                queue.append(child)
    if visited != len(remaining):
        raise ValidationError('Task dependencies cannot contain cycles.')


def plan_graph(plan):
    graph = {str(pk): set() for pk in Task.objects.filter(goal__plan=plan).values_list('pk', flat=True)}
    for task, parent in TaskDependency.objects.filter(task__goal__plan=plan).values_list('task_id', 'prerequisite_id'):
        graph[str(task)].add(str(parent))
    return graph


def replace_dependencies(task, ids):
    ids = [str(pk) for pk in ids]
    if len(ids) != len(set(ids)) or len(ids) > 50:
        raise ValidationError('Select at most 50 unique prerequisites.')
    graph = plan_graph(task.goal.plan)
    if any(pk not in graph for pk in ids):
        raise ValidationError('Every prerequisite must belong to this task\'s plan.')
    graph[str(task.pk)] = set(ids)
    validate_graph(graph)
    TaskDependency.objects.filter(task=task).delete()
    TaskDependency.objects.bulk_create([TaskDependency(task=task, prerequisite_id=pk) for pk in ids])


def scheduling_blockers(task, *, include_sessions=True):
    blockers = []
    if task.is_completed or task.goal.is_completed or task.goal.plan.status != 'active' or task.goal.plan.planning_state == 'completed':
        blockers.append('Task or goal is complete, or its plan is inactive.')
    if task.readiness != 'ready' or not task.completion_criteria.strip() or not task.effective_minutes or not valid_effort(task.effective_minutes):
        blockers.append('Task needs ready status, effort and completion criteria.')
    if task.progress_status == 'skipped':
        blockers.append('Task was skipped. Explicitly reopen it before scheduling.')
    if task.blocker_reason.strip():
        blockers.append(task.blocker_reason)
    if task.dependencies.filter(is_completed=False).exists():
        blockers.append('One or more prerequisite tasks are incomplete.')
    if task.planning_context_version is not None and task.planning_context_version != task.goal.plan.context_version:
        blockers.append('Planning context changed. Review the task and explicitly mark it ready again.')
    from goals.scheduling import needs_review
    if needs_review(task.goal.plan):
        blockers.append('Confirm scheduling_rules for saved timing and constraints before scheduling.')
    if include_sessions:
        from .guards import active_sessions
        if active_sessions(task).exists():
            blockers.append('This task already has an unfinished committed session.')
    return blockers

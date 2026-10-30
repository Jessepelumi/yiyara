from django.conf import settings
from datetime import date

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError as ApiValidationError

from tasks.models import Task, TaskDependency

from .models import Goal, Plan, PlanChange, PlanRevision, PlanQuestion


class InvalidPlanOperation(ValueError):
    pass


class PlanVersionConflict(RuntimeError):
    pass


def serialize_plan_snapshot(plan):
    goals = []
    for goal in plan.goals.prefetch_related("tasks").order_by("created_at"):
        goals.append(
            {
                "id": str(goal.id),
                "title": goal.title,
                "description": goal.description,
                "due_date": goal.due_date.isoformat() if goal.due_date else None,
                "is_completed": goal.is_completed,
                "tasks": [
                    {
                        "id": str(task.id),
                        "title": task.title,
                        "description": task.description,
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                        "is_completed": task.is_completed,
                        "progress_status": task.progress_status,
                        "remaining_minutes": task.remaining_minutes,
                        "progress_version": task.progress_version,
                        "estimated_minutes": task.estimated_minutes,
                        "completion_criteria": task.completion_criteria,
                        "readiness": task.readiness,
                        "blocker_reason": task.blocker_reason,
                        "planning_context_version": task.planning_context_version,
                        "dependencies": sorted(str(pk) for pk in task.dependencies.values_list('pk', flat=True)),
                    }
                    for task in goal.tasks.all().order_by("created_at")
                ],
            }
        )

    return {
        "id": str(plan.id),
        "title": plan.title,
        "raw_input": plan.raw_input,
        "status": plan.status,
        "version": plan.version,
        "context_version": plan.context_version,
        "planning_mode": plan.planning_mode,
        "planning_state": plan.planning_state,
        "context": plan.context,
        "context_provenance": plan.context_provenance,
        "waiting_reason": plan.waiting_reason,
        "questions": [
            {"id": str(q.pk), "text": q.text, "field": q.field, "required": q.required,
             "status": q.status, "answer": q.answer, "deferred_reason": q.deferred_reason,
             "source_message_id": str(q.source_message_id) if q.source_message_id else None}
            for q in plan.questions.all()
        ],
        "recent_task_outcomes": [
            {"id": str(item.pk), "task_id": str(item.task_id), "status": item.status, "notes": item.notes,
             "remaining_minutes": item.remaining_minutes, "spent_minutes": item.spent_minutes,
             "blocker_reason": item.blocker_reason, "created_at": item.created_at.isoformat()}
            for item in plan.task_outcomes.all()[:settings.OUTCOME_HISTORY_LIMIT]
        ],
        "goals": goals,
    }


def create_plan_revision(plan, summary):
    return PlanRevision.objects.update_or_create(
        plan=plan,
        version=plan.version,
        defaults={
            "summary": summary[:500],
            "snapshot": serialize_plan_snapshot(plan),
        },
    )[0]


def _parse_date(value, field_name):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise InvalidPlanOperation(f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidPlanOperation(f"{field_name} must use YYYY-MM-DD") from exc


def _text(value, field_name, *, required=False, max_length=None):
    if value is None:
        if required:
            raise InvalidPlanOperation(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise InvalidPlanOperation(f"{field_name} must be text")
    value = value.strip()
    if required and not value:
        raise InvalidPlanOperation(f"{field_name} is required")
    if max_length and len(value) > max_length:
        raise InvalidPlanOperation(f"{field_name} is too long")
    return value


def _goal_for_plan(plan, goal_id):
    try:
        return plan.goals.get(id=goal_id)
    except (Goal.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise InvalidPlanOperation("Goal does not belong to this board") from exc


def _task_for_plan(plan, task_id):
    try:
        return Task.objects.select_related("goal").get(id=task_id, goal__plan=plan)
    except (Task.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise InvalidPlanOperation("Task does not belong to this board") from exc


def _task_payload(payload, *, goal_due_date=None):
    title = _text(payload.get("title"), "Task title", required=True, max_length=255)
    description = _text(payload.get("description", ""), "Task description") or ""
    due_date = _parse_date(payload.get("due_date"), "Task due date")
    if goal_due_date and due_date and due_date > goal_due_date:
        raise InvalidPlanOperation("Task due date cannot exceed goal due date")
    return {
        "title": title,
        "description": description,
        "due_date": due_date.isoformat() if due_date else None,
    }


def normalize_plan_operations(plan, operations):
    from .planning import CONTEXT_ACTIONS, normalize_context_operation
    if not isinstance(operations, list) or len(operations) > settings.PLAN_MAX_CHANGE_OPERATIONS:
        raise InvalidPlanOperation(f"Changes must contain no more than {settings.PLAN_MAX_CHANGE_OPERATIONS} operations")

    normalized = []
    if any(isinstance(op, dict) and op.get('action') in ('add_task_batch', 'replan_tasks') for op in operations) and len(operations) != 1:
        raise InvalidPlanOperation('Task batches must be reviewed separately from context changes.')
    supported_actions = {
        "add_goal",
        "update_goal",
        "delete_goal",
        "add_task",
        "update_task",
        "delete_task",
    }

    for operation in operations:
        if not isinstance(operation, dict):
            raise InvalidPlanOperation("Each change must be an object")

        action = operation.get("action")
        if action == 'replan_tasks':
            from .replanning import normalize_replan
            try:
                normalized.append(normalize_replan(plan, operation))
            except ApiValidationError as exc:
                raise InvalidPlanOperation(str(exc.detail)) from exc
            continue
        if action == 'add_task_batch':
            from .task_planning import normalize_batch
            try:
                normalized.append(normalize_batch(plan, operation))
            except ApiValidationError as exc:
                raise InvalidPlanOperation(str(exc.detail)) from exc
            continue
        if action in CONTEXT_ACTIONS:
            try:
                normalized.append(normalize_context_operation(plan, operation))
            except ApiValidationError as exc:
                raise InvalidPlanOperation(str(exc.detail)) from exc
            continue
        if plan.planning_mode == "contextual":
            raise InvalidPlanOperation("This clarification stage accepts context and question changes only.")
        if action not in supported_actions:
            raise InvalidPlanOperation("Unsupported board operation")

        clean = {"action": action}

        if action == "add_goal":
            clean["title"] = _text(
                operation.get("title"), "Goal title", required=True, max_length=255
            )
            clean["description"] = (
                _text(operation.get("description", ""), "Goal description") or ""
            )
            due_date = _parse_date(operation.get("due_date"), "Goal due date")
            clean["due_date"] = due_date.isoformat() if due_date else None
            tasks = operation.get("tasks", [])
            if not isinstance(tasks, list) or len(tasks) > settings.PLAN_MAX_TASKS_PER_PROPOSAL:
                raise InvalidPlanOperation(f"A proposal can add at most {settings.PLAN_MAX_TASKS_PER_PROPOSAL} tasks")
            clean["tasks"] = [
                _task_payload(task, goal_due_date=due_date) for task in tasks
            ]

        elif action in {"update_goal", "delete_goal", "add_task"}:
            goal = _goal_for_plan(plan, operation.get("goal_id"))
            if action in ('update_goal', 'delete_goal'):
                from tasks.guards import ensure_goal_editable
                try:
                    ensure_goal_editable(goal)
                except ApiValidationError as exc:
                    raise InvalidPlanOperation(str(exc.detail)) from exc
            clean["goal_id"] = str(goal.id)

            if action == "update_goal":
                supplied = False
                if "title" in operation:
                    clean["title"] = _text(
                        operation["title"], "Goal title", required=True, max_length=255
                    )
                    supplied = True
                if "description" in operation:
                    clean["description"] = (
                        _text(operation["description"], "Goal description") or ""
                    )
                    supplied = True
                if "due_date" in operation:
                    due_date = _parse_date(operation["due_date"], "Goal due date")
                    if due_date and goal.tasks.filter(due_date__gt=due_date).exists():
                        raise InvalidPlanOperation(
                            "Goal due date cannot precede an existing task due date"
                        )
                    clean["due_date"] = due_date.isoformat() if due_date else None
                    supplied = True
                if "is_completed" in operation:
                    if not isinstance(operation["is_completed"], bool):
                        raise InvalidPlanOperation("Goal completion must be true or false")
                    clean["is_completed"] = operation["is_completed"]
                    supplied = True
                if not supplied:
                    raise InvalidPlanOperation("Goal update contains no fields")

            elif action == "add_task":
                clean.update(_task_payload(operation, goal_due_date=goal.due_date))
            elif action == 'delete_goal' and TaskDependency.objects.filter(
                prerequisite__goal=goal
            ).exclude(task__goal=goal).exists():
                raise InvalidPlanOperation('Other tasks depend on this goal; update their prerequisites before deleting it.')

        else:
            task = _task_for_plan(plan, operation.get("task_id"))
            if action == 'delete_task' and task.dependents.exists():
                raise InvalidPlanOperation('Other tasks depend on this task; update their prerequisites before deleting it.')
            from tasks.guards import ensure_editable
            try:
                ensure_editable(task, None if action == 'delete_task' else set(operation) - {'action', 'task_id'})
            except ApiValidationError as exc:
                raise InvalidPlanOperation(str(exc.detail)) from exc
            clean["task_id"] = str(task.id)

            if action == "update_task":
                supplied = False
                if "title" in operation:
                    clean["title"] = _text(
                        operation["title"], "Task title", required=True, max_length=255
                    )
                    supplied = True
                if "description" in operation:
                    clean["description"] = (
                        _text(operation["description"], "Task description") or ""
                    )
                    supplied = True
                if "due_date" in operation:
                    due_date = _parse_date(operation["due_date"], "Task due date")
                    if task.goal.due_date and due_date and due_date > task.goal.due_date:
                        raise InvalidPlanOperation(
                            "Task due date cannot exceed goal due date"
                        )
                    clean["due_date"] = due_date.isoformat() if due_date else None
                    supplied = True
                if "is_completed" in operation:
                    if not isinstance(operation["is_completed"], bool):
                        raise InvalidPlanOperation("Task completion must be true or false")
                    clean["is_completed"] = operation["is_completed"]
                    supplied = True
                if not supplied:
                    raise InvalidPlanOperation("Task update contains no fields")

        normalized.append(clean)

    legacy_goals = [op for op in normalized if op['action'] == 'add_goal']
    added_tasks = sum(len(op.get('tasks', [])) for op in legacy_goals) + sum(op['action'] == 'add_task' for op in normalized)
    if len(legacy_goals) > settings.PLAN_MAX_GOALS_PER_PROPOSAL or added_tasks > settings.PLAN_MAX_TASKS_PER_PROPOSAL:
        raise InvalidPlanOperation('Changes exceed the configured proposal limits.')
    return normalized


def create_plan_change(plan, scope_goal, summary, operations):
    normalized = normalize_plan_operations(plan, operations)
    if not normalized:
        return None
    return PlanChange.objects.create(
        plan=plan,
        scope_goal=scope_goal,
        summary=(summary or "Proposed board changes")[:500],
        operations=normalized,
        base_version=plan.version,
    )


def apply_plan_change(change, user):
    from .planning import CONTEXT_ACTIONS, apply_context_operation, validate_planning_state
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        change = (
            PlanChange.objects.select_for_update()
            .select_related("plan")
            .get(id=change.id, plan__user=user)
        )
        plan = change.plan

        if change.status != PlanChange.Status.PROPOSED:
            raise InvalidPlanOperation("Change is no longer pending")
        if plan.version != change.base_version:
            raise PlanVersionConflict(
                "Board changed after this proposal. Ask Yiyara to prepare it again."
            )

        operations = normalize_plan_operations(plan, change.operations)
        for operation in operations:
            action = operation["action"]
            if action == 'replan_tasks':
                from .replanning import apply_replan
                try:
                    apply_replan(plan, operation)
                except ApiValidationError as exc:
                    raise InvalidPlanOperation(str(exc.detail)) from exc
                continue
            if action == 'add_task_batch':
                from .task_planning import apply_batch
                try:
                    apply_batch(plan, operation)
                except ApiValidationError as exc:
                    raise InvalidPlanOperation(str(exc.detail)) from exc
                continue

            if action in CONTEXT_ACTIONS:
                try:
                    apply_context_operation(plan, operation)
                except ApiValidationError as exc:
                    raise InvalidPlanOperation(str(exc.detail)) from exc
                continue

            if action == "add_goal":
                goal = Goal.objects.create(
                    plan=plan,
                    user=plan.user,
                    raw_input=plan.raw_input,
                    title=operation["title"],
                    description=operation["description"],
                    due_date=operation["due_date"],
                )
                Task.objects.bulk_create(
                    [
                        Task(
                            goal=goal,
                            title=task["title"],
                            description=task["description"],
                            due_date=task["due_date"],
                        )
                        for task in operation["tasks"]
                    ]
                )
            elif action == "update_goal":
                goal = _goal_for_plan(plan, operation["goal_id"])
                for field in ("title", "description", "due_date", "is_completed"):
                    if field in operation:
                        setattr(goal, field, operation[field])
                goal.save()
            elif action == "delete_goal":
                if plan.goals.count() <= 1:
                    raise InvalidPlanOperation("A board must retain at least one goal")
                _goal_for_plan(plan, operation["goal_id"]).delete()
            elif action == "add_task":
                goal = _goal_for_plan(plan, operation["goal_id"])
                Task.objects.create(
                    goal=goal,
                    title=operation["title"],
                    description=operation["description"],
                    due_date=operation["due_date"],
                )
            elif action == "update_task":
                task = _task_for_plan(plan, operation["task_id"])
                for field in ("title", "description", "due_date"):
                    if field in operation:
                        setattr(task, field, operation[field])
                task.save()
                if 'is_completed' in operation:
                    from tasks.outcomes import record_completion_toggle
                    record_completion_toggle(task, plan, operation['is_completed'])
            elif action == "delete_task":
                _task_for_plan(plan, operation["task_id"]).delete()

        if plan.planning_mode == "contextual":
            try:
                validate_planning_state(plan)
            except ApiValidationError as exc:
                raise InvalidPlanOperation(str(exc.detail)) from exc
        plan.version += 1
        if any(op['action'] in CONTEXT_ACTIONS for op in operations):
            plan.context_version += 1
        plan.save(update_fields=["version", "context_version", "updated_at"])
        change.status = PlanChange.Status.APPLIED
        change.applied_at = timezone.now()
        change.save(update_fields=["status", "applied_at", "updated_at"])
        create_plan_revision(plan, change.summary)
        return plan, change


def reject_plan_change(change, user):
    updated = PlanChange.objects.filter(
        id=change.id,
        plan__user=user,
        status=PlanChange.Status.PROPOSED,
    ).update(status=PlanChange.Status.REJECTED, updated_at=timezone.now())
    if not updated:
        raise InvalidPlanOperation("Change is no longer pending")
    change.refresh_from_db()
    return change


def restore_plan_revision(revision, user):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        plan = Plan.objects.select_for_update().get(
            id=revision.plan_id,
            user=user,
        )
        snapshot = revision.snapshot
        snapshot_goals = snapshot.get("goals", [])
        if not snapshot_goals and snapshot.get("planning_mode", "legacy") != "contextual":
            raise InvalidPlanOperation("Revision contains no goals")

        goal_ids = [goal["id"] for goal in snapshot_goals]
        # Rebuild edges only after every restored task exists; rollback restores
        # the previous graph if a snapshot is invalid.
        TaskDependency.objects.filter(task__goal__plan=plan).delete()
        plan.goals.exclude(id__in=goal_ids).delete()

        for goal_data in snapshot_goals:
            if Goal.objects.filter(id=goal_data["id"]).exclude(plan=plan).exists():
                raise InvalidPlanOperation("Revision goal ID belongs to another board")
            goal, _ = Goal.objects.update_or_create(
                id=goal_data["id"],
                defaults={
                    "plan": plan,
                    "user": plan.user,
                    "raw_input": snapshot.get("raw_input", plan.raw_input),
                    "title": goal_data["title"],
                    "description": goal_data.get("description", ""),
                    "due_date": goal_data.get("due_date"),
                    "is_completed": goal_data.get("is_completed", False),
                },
            )
            task_data = goal_data.get("tasks", [])
            task_ids = [task["id"] for task in task_data]
            goal.tasks.exclude(id__in=task_ids).delete()
            for item in task_data:
                if Task.objects.filter(id=item["id"]).exclude(goal__plan=plan).exists():
                    raise InvalidPlanOperation("Revision task ID belongs to another board")
                Task.objects.update_or_create(
                    id=item["id"],
                    defaults={
                        "goal": goal,
                        "title": item["title"],
                        "description": item.get("description", ""),
                        "due_date": item.get("due_date"),
                        "is_completed": item.get("is_completed", False),
                        "progress_status": item.get("progress_status", 'completed' if item.get('is_completed') else 'not_started'),
                        "remaining_minutes": item.get("remaining_minutes"),
                        "progress_version": item.get("progress_version", 0),
                        "estimated_minutes": item.get("estimated_minutes"),
                        "completion_criteria": item.get("completion_criteria", ""),
                        "readiness": item.get("readiness", "draft"),
                        "blocker_reason": item.get("blocker_reason", ""),
                        "planning_context_version": item.get("planning_context_version"),
                    },
                )

        from tasks.outcomes import preserve_reported_progress
        for task in Task.objects.filter(goal__plan=plan):
            preserve_reported_progress(task, plan)

        from tasks.dependencies import replace_dependencies
        try:
            for goal_data in snapshot_goals:
                for item in goal_data.get('tasks', []):
                    task = Task.objects.select_related('goal__plan').get(pk=item['id'])
                    replace_dependencies(task, item.get('dependencies', []))
        except ApiValidationError as exc:
            raise InvalidPlanOperation('Revision contains invalid task dependencies.') from exc

        plan.title = snapshot.get("title", plan.title)
        plan.raw_input = snapshot.get("raw_input", plan.raw_input)
        plan.status = snapshot.get("status", plan.status)
        plan.planning_mode = snapshot.get("planning_mode", "legacy")
        plan.planning_state = snapshot.get("planning_state", "actionable")
        plan.context = snapshot.get("context", {})
        plan.context_provenance = snapshot.get("context_provenance", {})
        plan.waiting_reason = snapshot.get("waiting_reason", "")
        questions = snapshot.get("questions", [])
        plan.questions.exclude(pk__in=[q["id"] for q in questions]).delete()
        for question in questions:
            if PlanQuestion.objects.filter(pk=question["id"]).exclude(plan=plan).exists():
                raise InvalidPlanOperation("Revision question belongs to another plan")
            source_id = question.get("source_message_id")
            if source_id:
                from conversations.models import Message

                if not Message.objects.filter(pk=source_id, conversation__plan=plan, role="user").exists():
                    raise InvalidPlanOperation("Revision question has invalid evidence")
            PlanQuestion.objects.update_or_create(pk=question["id"], defaults={
                "plan": plan, **{key: question[key] for key in
                                 ("text", "field", "required", "status", "answer", "deferred_reason")},
                "source_message_id": source_id,
            })
        plan.version += 1
        plan.context_version += 1
        plan.save(
            update_fields=["title", "raw_input", "status", "version", "updated_at",
                           "planning_mode", "planning_state", "context", "context_provenance", "waiting_reason", "context_version"]
        )
        create_plan_revision(plan, f"Restored version {revision.version}")
        return plan

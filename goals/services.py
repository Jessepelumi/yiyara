from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from tasks.models import Task

from .models import Goal, Plan, PlanChange, PlanRevision


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
    if not isinstance(operations, list) or len(operations) > 25:
        raise InvalidPlanOperation("Changes must contain no more than 25 operations")

    normalized = []
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
            if not isinstance(tasks, list) or len(tasks) > 50:
                raise InvalidPlanOperation("A goal can contain no more than 50 tasks")
            clean["tasks"] = [
                _task_payload(task, goal_due_date=due_date) for task in tasks
            ]

        elif action in {"update_goal", "delete_goal", "add_task"}:
            goal = _goal_for_plan(plan, operation.get("goal_id"))
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

        else:
            task = _task_for_plan(plan, operation.get("task_id"))
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
    with transaction.atomic():
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
                for field in ("title", "description", "due_date", "is_completed"):
                    if field in operation:
                        setattr(task, field, operation[field])
                task.save()
            elif action == "delete_task":
                _task_for_plan(plan, operation["task_id"]).delete()

        plan.version += 1
        plan.save(update_fields=["version", "updated_at"])
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
        plan = Plan.objects.select_for_update().get(
            id=revision.plan_id,
            user=user,
        )
        snapshot = revision.snapshot
        snapshot_goals = snapshot.get("goals", [])
        if not snapshot_goals:
            raise InvalidPlanOperation("Revision contains no goals")

        goal_ids = [goal["id"] for goal in snapshot_goals]
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
                    },
                )

        plan.title = snapshot.get("title", plan.title)
        plan.raw_input = snapshot.get("raw_input", plan.raw_input)
        plan.status = snapshot.get("status", plan.status)
        plan.version += 1
        plan.save(
            update_fields=["title", "raw_input", "status", "version", "updated_at"]
        )
        create_plan_revision(plan, f"Restored version {revision.version}")
        return plan

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_plans_for_existing_goals(apps, schema_editor):
    Goal = apps.get_model("goals", "Goal")
    Plan = apps.get_model("goals", "Plan")
    PlanRevision = apps.get_model("goals", "PlanRevision")
    Task = apps.get_model("tasks", "Task")

    for goal in Goal.objects.all().iterator():
        raw_input = goal.raw_input or goal.title
        plan = Plan.objects.create(
            user_id=goal.user_id,
            title=raw_input[:255],
            raw_input=raw_input,
            status="active",
            version=1,
        )
        goal.plan_id = plan.id
        goal.save(update_fields=["plan"])

        tasks = []
        for task in Task.objects.filter(goal_id=goal.id).order_by("created_at"):
            tasks.append(
                {
                    "id": str(task.id),
                    "title": task.title,
                    "description": task.description,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "is_completed": task.is_completed,
                }
            )

        PlanRevision.objects.create(
            plan_id=plan.id,
            version=1,
            summary="Migrated existing goal",
            snapshot={
                "id": str(plan.id),
                "title": plan.title,
                "raw_input": plan.raw_input,
                "status": plan.status,
                "version": 1,
                "goals": [
                    {
                        "id": str(goal.id),
                        "title": goal.title,
                        "description": goal.description,
                        "due_date": goal.due_date.isoformat() if goal.due_date else None,
                        "is_completed": goal.is_completed,
                        "tasks": tasks,
                    }
                ],
            },
        )


def unlink_plans(apps, schema_editor):
    Goal = apps.get_model("goals", "Goal")
    Goal.objects.update(plan=None)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("goals", "0001_initial"),
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Plan",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("raw_input", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("completed", "Completed"),
                            ("archived", "Archived"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=20,
                    ),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plans",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.AddField(
            model_name="goal",
            name="plan",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="goals",
                to="goals.plan",
            ),
        ),
        migrations.CreateModel(
            name="PlanChange",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("proposed", "Proposed"),
                            ("applied", "Applied"),
                            ("rejected", "Rejected"),
                        ],
                        db_index=True,
                        default="proposed",
                        max_length=20,
                    ),
                ),
                ("summary", models.CharField(max_length=500)),
                ("operations", models.JSONField(default=list)),
                ("base_version", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="changes",
                        to="goals.plan",
                    ),
                ),
                (
                    "scope_goal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="proposed_changes",
                        to="goals.goal",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="PlanRevision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("version", models.PositiveIntegerField()),
                ("summary", models.CharField(blank=True, max_length=500)),
                ("snapshot", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="revisions",
                        to="goals.plan",
                    ),
                ),
            ],
            options={
                "ordering": ["-version"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("plan", "version"),
                        name="unique_plan_revision_version",
                    )
                ],
            },
        ),
        migrations.RunPython(create_plans_for_existing_goals, unlink_plans),
        migrations.AlterField(
            model_name="goal",
            name="plan",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="goals",
                to="goals.plan",
            ),
        ),
        migrations.AlterModelOptions(
            name="goal",
            options={"ordering": ["created_at"]},
        ),
    ]

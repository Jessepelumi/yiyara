import uuid
from django.db import models
from goals.models import Goal # link to Goal model

class Task(models.Model):
    # UUID as primary key
    id = models.UUIDField(
        primary_key=True, 
        default=uuid.uuid4, 
        editable=False
    )

    # link to a goal
    goal = models.ForeignKey(
        Goal,
        on_delete=models.CASCADE,
        related_name='tasks'
    )

    # required title
    title = models.CharField(max_length=255)

    # optional description
    description = models.TextField(blank=True)

    due_date = models.DateField(
        null=True, 
        blank=True, 
        db_index=True
    )
    is_completed = models.BooleanField(default=False)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    completion_criteria = models.TextField(blank=True)
    readiness = models.CharField(
        max_length=10, choices=[("draft", "Draft"), ("ready", "Ready"), ("blocked", "Blocked")],
        default="draft",
    )
    blocker_reason = models.TextField(blank=True)
    progress_status = models.CharField(max_length=12, default='not_started', choices=[
        ('not_started', 'Not started'), ('partial', 'Partial'), ('completed', 'Completed'),
        ('blocked', 'Blocked'), ('skipped', 'Skipped'),
    ])
    remaining_minutes = models.PositiveIntegerField(null=True, blank=True)
    progress_version = models.PositiveIntegerField(default=0)
    planning_context_version = models.PositiveIntegerField(null=True, blank=True)
    dependencies = models.ManyToManyField(
        'self', symmetrical=False, through='TaskDependency',
        through_fields=('task', 'prerequisite'), related_name='dependents', blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ordering for tasks
    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.title

    @property
    def effective_minutes(self):
        return self.remaining_minutes if self.remaining_minutes is not None else self.estimated_minutes


class TaskDependency(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='prerequisite_links')
    prerequisite = models.ForeignKey(Task, on_delete=models.RESTRICT, related_name='dependent_links')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['task', 'prerequisite'], name='unique_task_prerequisite'),
            models.CheckConstraint(condition=~models.Q(task=models.F('prerequisite')), name='task_cannot_depend_on_itself'),
        ]


class TaskOutcome(models.Model):
    """Append-only user reports; task identity survives deletion and revision restore."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey('goals.Plan', on_delete=models.CASCADE, related_name='task_outcomes')
    task_id = models.UUIDField(db_index=True)
    task_title = models.CharField(max_length=255)
    client_id = models.UUIDField()
    status = models.CharField(max_length=12, choices=[
        ('completed', 'Completed'), ('partial', 'Partial'), ('blocked', 'Blocked'),
        ('skipped', 'Skipped'), ('reopened', 'Reopened'),
    ])
    notes = models.TextField(blank=True)
    remaining_minutes = models.PositiveIntegerField(null=True, blank=True)
    spent_minutes = models.PositiveIntegerField(null=True, blank=True)
    blocker_reason = models.CharField(max_length=2000, blank=True)
    session_id = models.UUIDField(null=True, blank=True)
    request_data = models.JSONField(default=dict)
    task_state = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['plan', 'client_id'], name='unique_plan_outcome_client_id')]

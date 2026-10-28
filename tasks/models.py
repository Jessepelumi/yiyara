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


class TaskDependency(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='prerequisite_links')
    prerequisite = models.ForeignKey(Task, on_delete=models.RESTRICT, related_name='dependent_links')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['task', 'prerequisite'], name='unique_task_prerequisite'),
            models.CheckConstraint(condition=~models.Q(task=models.F('prerequisite')), name='task_cannot_depend_on_itself'),
        ]

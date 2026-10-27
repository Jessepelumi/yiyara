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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ordering for tasks
    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.title

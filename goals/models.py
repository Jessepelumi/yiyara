import uuid
from django.db import models
from django.conf import settings


class Plan(models.Model):
    """One user ambition and its shared console drawing board."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="plans",
    )
    title = models.CharField(max_length=255)
    raw_input = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)
    context_version = models.PositiveIntegerField(default=1)
    planning_mode = models.CharField(max_length=12, default="legacy", choices=[
        ("legacy", "Legacy decomposition"), ("contextual", "Context-aware planning"),
    ])
    planning_state = models.CharField(max_length=12, default="actionable", choices=[
        ("clarifying", "Clarifying"), ("actionable", "Actionable"),
        ("waiting", "Waiting"), ("completed", "Completed"),
    ])
    context = models.JSONField(default=dict, blank=True)
    context_provenance = models.JSONField(default=dict, blank=True)
    waiting_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

class Goal(models.Model):

    # use UUID as primary key
    id = models.UUIDField(
        primary_key=True, 
        default=uuid.uuid4, 
        editable=False
    )

    # link each goal to a user
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='goals'
    )

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="goals",
    )

    # system-generated title & description
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    # smartified input from AI layer
    raw_input = models.TextField(blank=True)

    # optional due date
    due_date = models.DateField(
        null=True, 
        blank=True, 
        db_index=True
    )

    # completion status
    is_completed = models.BooleanField(default=False)

    # timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # goal ordering
    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.title


class PlanRevision(models.Model):
    """Immutable board snapshot created after each accepted mutation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    version = models.PositiveIntegerField()
    summary = models.CharField(max_length=500, blank=True)
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "version"],
                name="unique_plan_revision_version",
            )
        ]


class PlanChange(models.Model):
    """AI-proposed board operations awaiting user approval."""

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="changes",
    )
    scope_goal = models.ForeignKey(
        Goal,
        on_delete=models.SET_NULL,
        related_name="proposed_changes",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PROPOSED,
        db_index=True,
    )
    summary = models.CharField(max_length=500)
    operations = models.JSONField(default=list)
    base_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class PlanQuestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="questions")
    text = models.CharField(max_length=2000)
    field = models.CharField(max_length=20, choices=[
        ("desired_outcome", "Desired outcome"), ("starting_point", "Starting point"),
        ("timing", "Timing"), ("constraints", "Constraints"), ("other", "Other"),
    ])
    required = models.BooleanField(default=True)
    status = models.CharField(max_length=10, default="open", choices=[
        ("open", "Open"), ("answered", "Answered"), ("deferred", "Deferred"),
    ])
    answer = models.TextField(blank=True)
    deferred_reason = models.CharField(max_length=2000, blank=True)
    source_message = models.ForeignKey("conversations.Message", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

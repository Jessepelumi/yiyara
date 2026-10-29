import uuid

from django.conf import settings
from django.db import models


class CalendarConnection(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    google_sub = models.CharField(max_length=255)
    email = models.EmailField()
    credentials = models.TextField()  # Fernet ciphertext only.
    expires_at = models.DateTimeField()
    scopes = models.JSONField(default=list)
    source_calendars = models.JSONField(default=list)
    destination_calendar = models.CharField(max_length=1024, blank=True)
    reconnect_required = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class CalendarAuthorization(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    state_hash = models.CharField(max_length=64, unique=True)
    verifier = models.TextField()  # Encrypted PKCE verifier and OIDC nonce.
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)


class SchedulingPreferences(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    timezone = models.CharField(max_length=100)
    working_hours = models.JSONField(default=list)
    preferred_session_minutes = models.PositiveIntegerField(default=60)
    updated_at = models.DateTimeField(auto_now=True)


class ProtectedPeriod(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=10, choices=[("one_off", "One off"), ("weekly", "Weekly")])
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    weekdays = models.JSONField(default=list, blank=True)
    all_day = models.BooleanField(default=False)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="one_off", starts_at__isnull=False, ends_at__isnull=False,
                             ends_at__gt=models.F("starts_at"))
                    | models.Q(kind="weekly", starts_at__isnull=True, ends_at__isnull=True)
                ),
                name="protected_period_valid_kind_and_range",
            ),
        ]


class WorkSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    task = models.ForeignKey("tasks.Task", null=True, on_delete=models.SET_NULL, related_name="sessions")
    status = models.CharField(max_length=12, default="proposed", choices=[
        ("proposed", "Proposed"), ("publishing", "Publishing"),
        ("published", "Published"), ("rejected", "Rejected"), ("cancelled", "Cancelled"),
    ])
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    destination_calendar = models.CharField(max_length=1024)
    google_sub = models.CharField(max_length=255)
    google_event_id = models.CharField(max_length=64, unique=True)
    task_snapshot = models.JSONField(default=dict)
    event_payload = models.JSONField(default=dict)
    last_error = models.CharField(max_length=200, blank=True)
    approved_at = models.DateTimeField(null=True)
    published_at = models.DateTimeField(null=True)
    publish_attempt = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    management_version = models.PositiveIntegerField(default=0)
    cancelled_at = models.DateTimeField(null=True)
    conflicts = models.JSONField(default=list, blank=True)
    conflict_token = models.CharField(max_length=64, blank=True)
    overlap_approved = models.BooleanField(default=False)
    overlap_history = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(ends_at__gt=models.F("starts_at")), name="session_positive_duration"),
            models.UniqueConstraint(fields=["task"], condition=models.Q(status__in=["publishing", "published"]),
                                    name="one_committed_session_per_task"),
        ]


class SessionChange(models.Model):
    """Reviewable, durable intent to mutate a published Google event."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(WorkSession, on_delete=models.CASCADE, related_name='changes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    client_id = models.UUIDField()
    kind = models.CharField(max_length=12, choices=[('reschedule', 'Reschedule'), ('cancel', 'Cancel')])
    status = models.CharField(max_length=10, default='proposed', choices=[
        ('proposed', 'Proposed'), ('applying', 'Applying'), ('applied', 'Applied'), ('rejected', 'Rejected'),
        ('conflicted', 'Conflicted'),
    ])
    base_version = models.PositiveIntegerField()
    request_data = models.JSONField(default=dict)
    before_event = models.JSONField(default=dict)
    task_snapshot = models.JSONField(default=dict)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True)
    applied_at = models.DateTimeField(null=True)
    attempt = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    conflicts = models.JSONField(default=list, blank=True)
    conflict_token = models.CharField(max_length=64, blank=True)
    overlap_approved = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'client_id'], name='unique_session_change_client_id'),
            models.UniqueConstraint(fields=['session'], condition=models.Q(status__in=['proposed', 'applying']),
                                    name='one_active_session_change'),
            models.CheckConstraint(condition=(models.Q(kind='cancel', starts_at__isnull=True, ends_at__isnull=True)
                | models.Q(kind='reschedule', starts_at__isnull=False, ends_at__isnull=False, ends_at__gt=models.F('starts_at'))),
                name='session_change_valid_range'),
        ]

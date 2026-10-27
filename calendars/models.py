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

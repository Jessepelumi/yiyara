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

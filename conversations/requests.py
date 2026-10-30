"""Durable AI request leases and fencing for retries after process interruption.

Call claim/ensure_current under the shared user lock and transaction.
"""
import uuid
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from .models import Message


def retryable(message):
    if message.metadata.get('ai_status') == 'failed':
        return True
    if message.metadata.get('ai_status') != 'pending':
        return False
    expiry = message.ai_lease_until or message.created_at + timedelta(seconds=settings.AI_REQUEST_LEASE_SECONDS)
    return expiry <= timezone.now()


def claim(message, metadata=None):
    message.metadata = {**message.metadata, **(metadata or {}), 'ai_status': 'pending'}
    message.ai_attempt = uuid.uuid4()
    message.ai_lease_until = timezone.now() + timedelta(seconds=settings.AI_REQUEST_LEASE_SECONDS)
    message.save(update_fields=['metadata', 'ai_attempt', 'ai_lease_until'])


def ensure_current(message):
    from goals.services import PlanVersionConflict
    if not Message.objects.filter(pk=message.pk, ai_attempt=message.ai_attempt, ai_lease_until__gt=timezone.now()).exists():
        raise PlanVersionConflict('AI request lease expired or was superseded. Retry the same client_id.')


def complete(message):
    ensure_current(message)
    message.metadata = {**message.metadata, 'ai_status': 'completed'}
    message.ai_lease_until = None
    message.save(update_fields=['metadata', 'ai_lease_until'])


def fail(message):
    Message.objects.filter(pk=message.pk, ai_attempt=message.ai_attempt).update(
        metadata={**message.metadata, 'ai_status': 'failed'}, ai_lease_until=None)

import uuid
from django.db import models
from django.conf import settings
from django.db.models import Q
from goals.models import Goal, Plan

class Conversation(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    plan = models.OneToOneField(
        Plan,
        on_delete=models.CASCADE,
        related_name="conversation",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Board chat for {self.plan.title} - {self.user}"
    
class Message(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    class Role(models.TextChoices):
        USER = 'user', 'User'
        ASSISTANT = 'assistant', 'Assistant'
        SYSTEM = 'system', 'System'

    conversation = models.ForeignKey(
        Conversation, 
        on_delete=models.CASCADE, 
        related_name='messages'
    )
    role = models.CharField(
        max_length=10, 
        choices=Role.choices, 
        default=Role.USER
    )
    content = models.TextField()
    scope_goal = models.ForeignKey(
        Goal,
        on_delete=models.SET_NULL,
        related_name="scoped_messages",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    client_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "client_id"],
                condition=Q(client_id__isnull=False),
                name="unique_conversation_client_message",
            )
        ]

    def __str__(self):
        return f"{self.role}: {self.content[:30]}..."

from rest_framework import serializers

from .models import Conversation, Message


class MessageSerializer(serializers.ModelSerializer):
    scope_goal_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = Message
        fields = [
            "id",
            "role",
            "content",
            "scope_goal_id",
            "metadata",
            "client_id",
            "created_at",
        ]


class PlanMessageRequestSerializer(serializers.Serializer):
    content = serializers.CharField(max_length=10_000, trim_whitespace=True)
    scope_goal_id = serializers.UUIDField(required=False, allow_null=True)
    client_id = serializers.UUIDField(required=False)
    plan_version = serializers.IntegerField(required=False, min_value=1)


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta:
        model = Conversation
        fields = ["id", "plan", "user", "messages", "created_at", "updated_at"]

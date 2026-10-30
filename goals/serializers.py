from rest_framework import serializers
from django.core.exceptions import ObjectDoesNotExist
from .models import Goal, Plan, PlanChange, PlanRevision, PlanQuestion
from tasks.serializers import TaskSerializers


class GoalSerializer(serializers.ModelSerializer):
    tasks = TaskSerializers(many=True, read_only=True)
    plan_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Goal
        fields = [
            'id',
            'plan_id',
            'title',
            'description',
            'due_date',
            'is_completed',
            'tasks',
        ]


class PlanQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanQuestion
        fields = ["id", "text", "field", "required", "status", "answer", "deferred_reason", "source_message_id"]
        read_only_fields = fields


class AmbitionSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=10000)
    title = serializers.CharField(max_length=255, required=False)


class PlanSerializer(serializers.ModelSerializer):
    goals = GoalSerializer(many=True, read_only=True)
    conversation_id = serializers.SerializerMethodField()
    questions = PlanQuestionSerializer(many=True, read_only=True)
    scheduling_rules_review_required = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            "id",
            "title",
            "raw_input",
            "status",
            "version",
            "context_version",
            "conversation_id",
            "goals",
            "created_at",
            "updated_at",
            "planning_mode", "planning_state", "context", "context_provenance", "waiting_reason", "questions", "scheduling_rules_review_required",
        ]

    def get_scheduling_rules_review_required(self, obj):
        from .scheduling import needs_review
        return needs_review(obj)

    def get_conversation_id(self, obj):
        try:
            return obj.conversation.id
        except ObjectDoesNotExist:
            return None


class PlanSummarySerializer(serializers.ModelSerializer):
    goal_count = serializers.IntegerField(read_only=True)
    task_count = serializers.IntegerField(read_only=True)
    first_goal_id = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            "id",
            "title",
            "status",
            "version",
            "goal_count",
            "task_count",
            "first_goal_id",
            "created_at",
            "updated_at",
            "planning_mode", "planning_state",
        ]

    def get_first_goal_id(self, obj):
        goal = obj.goals.order_by("created_at").only("id").first()
        return goal.id if goal else None


class PlanChangeSerializer(serializers.ModelSerializer):
    scope_goal_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = PlanChange
        fields = [
            "id",
            "plan_id",
            "scope_goal_id",
            "status",
            "summary",
            "operations",
            "base_version",
            "created_at",
            "updated_at",
            "applied_at",
        ]


class PlanRevisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanRevision
        fields = ["id", "version", "summary", "created_at"]

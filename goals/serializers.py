from rest_framework import serializers
from django.core.exceptions import ObjectDoesNotExist
from .models import Goal, Plan, PlanChange, PlanRevision
from tasks.serializers import TaskSerializers


class TaskDecompositionSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, trim_whitespace=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    due_date = serializers.DateField(required=False, allow_null=True, default=None)


class GoalDecompositionSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, trim_whitespace=True)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    due_date = serializers.DateField(required=False, allow_null=True, default=None)
    tasks = TaskDecompositionSerializer(many=True, allow_empty=False, max_length=50)

    def validate(self, attrs):
        goal_due_date = attrs.get("due_date")
        if goal_due_date:
            for task in attrs["tasks"]:
                task_due_date = task.get("due_date")
                if task_due_date and task_due_date > goal_due_date:
                    raise serializers.ValidationError(
                        "Task due dates cannot be later than the goal due date."
                    )
        return attrs


class DecomposeGoalRequestSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=10_000, trim_whitespace=True)


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


class PlanSerializer(serializers.ModelSerializer):
    goals = GoalSerializer(many=True, read_only=True)
    conversation_id = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            "id",
            "title",
            "raw_input",
            "status",
            "version",
            "conversation_id",
            "goals",
            "created_at",
            "updated_at",
        ]

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
        ]

    def get_first_goal_id(self, obj):
        goal = obj.goals.order_by("created_at").only("id").first()
        return goal.id if goal else None


class PreviewPlanSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    raw_input = serializers.CharField()
    goals = GoalDecompositionSerializer(many=True)


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

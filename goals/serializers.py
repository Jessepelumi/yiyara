from rest_framework import serializers
from .models import Goal
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

    class Meta:
        model = Goal
        fields = ['id', 'title', 'description', 'due_date', 'is_completed', 'tasks']

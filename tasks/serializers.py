from rest_framework import serializers
from .models import Task

class TaskSerializers(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = ['id', 'title', 'description', 'is_completed', 'due_date',
                  'estimated_minutes', 'completion_criteria', 'readiness']


class TaskWriteSerializer(TaskSerializers):
    estimated_minutes = serializers.IntegerField(min_value=5, max_value=480, allow_null=True, required=False)
    completion_criteria = serializers.CharField(max_length=5000, allow_blank=True, required=False)

    class Meta(TaskSerializers.Meta):
        read_only_fields = ['id']

    def validate(self, attrs):
        def value(name, default=None):
            return attrs.get(name, getattr(self.instance, name, default))

        if value('readiness', 'draft') == 'ready' and (
            not value('estimated_minutes') or not value('completion_criteria', '').strip()
        ):
            raise serializers.ValidationError('Ready tasks require estimated_minutes and completion_criteria.')
        goal = self.context['goal']
        if goal.due_date and value('due_date') and value('due_date') > goal.due_date:
            raise serializers.ValidationError('Task due date cannot exceed the goal due date.')
        return attrs

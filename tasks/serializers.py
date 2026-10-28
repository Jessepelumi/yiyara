from rest_framework import serializers
from .models import Task

class TaskSerializers(serializers.ModelSerializer):
    dependencies = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    scheduling_blockers = serializers.SerializerMethodField()
    is_schedulable = serializers.SerializerMethodField()

    def get_scheduling_blockers(self, obj):
        from .dependencies import scheduling_blockers
        return scheduling_blockers(obj)

    def get_is_schedulable(self, obj):
        return not self.get_scheduling_blockers(obj)

    class Meta:
        model = Task
        fields = ['id', 'title', 'description', 'is_completed', 'due_date',
                  'estimated_minutes', 'completion_criteria', 'readiness', 'blocker_reason',
                  'planning_context_version', 'dependencies', 'scheduling_blockers', 'is_schedulable']


class TaskWriteSerializer(TaskSerializers):
    estimated_minutes = serializers.IntegerField(min_value=5, max_value=480, allow_null=True, required=False)
    completion_criteria = serializers.CharField(max_length=5000, allow_blank=True, required=False)
    blocker_reason = serializers.CharField(max_length=2000, allow_blank=True, required=False)
    dependency_ids = serializers.ListField(child=serializers.UUIDField(), max_length=50, required=False, write_only=True)

    class Meta(TaskSerializers.Meta):
        fields = [*TaskSerializers.Meta.fields, 'dependency_ids']
        read_only_fields = ['id', 'planning_context_version']

    def create(self, validated_data):
        from .dependencies import replace_dependencies
        ids = validated_data.pop('dependency_ids', [])
        goal = self.context['goal']
        if goal.plan.planning_mode == 'contextual':
            validated_data['planning_context_version'] = goal.plan.context_version
        task = super().create(validated_data)
        replace_dependencies(task, ids)
        return task

    def update(self, instance, validated_data):
        from .dependencies import replace_dependencies
        ids = validated_data.pop('dependency_ids', None)
        if validated_data.get('readiness') == 'ready' and instance.goal.plan.planning_mode == 'contextual':
            validated_data['planning_context_version'] = instance.goal.plan.context_version
        task = super().update(instance, validated_data)
        if ids is not None:
            replace_dependencies(task, ids)
        return task

    def validate(self, attrs):
        def value(name, default=None):
            return attrs.get(name, getattr(self.instance, name, default))

        if value('readiness', 'draft') == 'ready' and (
            not value('estimated_minutes') or not value('completion_criteria', '').strip()
        ):
            raise serializers.ValidationError('Ready tasks require estimated_minutes and completion_criteria.')
        goal = self.context['goal']
        if value('readiness', 'draft') == 'ready' and value('blocker_reason', '').strip():
            raise serializers.ValidationError('Clear the external blocker before marking a task ready.')
        if goal.due_date and value('due_date') and value('due_date') > goal.due_date:
            raise serializers.ValidationError('Task due date cannot exceed the goal due date.')
        return attrs

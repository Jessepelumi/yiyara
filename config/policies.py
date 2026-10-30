"""Shared runtime policy: settings own defaults, services and prompts consume them."""
from django.conf import settings
from rest_framework import serializers


class EffortField(serializers.IntegerField):
    def run_validation(self, data=serializers.empty):
        value = super().run_validation(data)
        if value is not None and not settings.MIN_SESSION_MINUTES <= value <= settings.MAX_SESSION_MINUTES:
            raise serializers.ValidationError(f'Use {settings.MIN_SESSION_MINUTES}–{settings.MAX_SESSION_MINUTES} minutes.')
        return value


def valid_effort(value):
    return value is not None and settings.MIN_SESSION_MINUTES <= value <= settings.MAX_SESSION_MINUTES


def render_prompt(template):
    values = {
        'max_goals': settings.PLAN_MAX_GOALS_PER_PROPOSAL,
        'max_tasks': settings.PLAN_MAX_TASKS_PER_PROPOSAL,
        'min_minutes': settings.MIN_SESSION_MINUTES,
        'max_minutes': settings.MAX_SESSION_MINUTES,
        'horizon_days': settings.PLAN_NEAR_TERM_DAYS,
        'max_questions': settings.PLAN_MAX_QUESTIONS_PER_TURN,
    }
    for name, value in values.items():
        template = template.replace('{{' + name + '}}', str(value))
    return template

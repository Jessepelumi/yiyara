from django.urls import path
from .planning_views import PlanContextView, QuestionAnswerView, TaskPlanProposalView, ReplanProposalView, UpgradeLegacyPlanView
from .views import (
    DeleteGoalView,
    GoalListView,
    PlanChangeListView,
    PlanDetailView,
    PlanListView,
    PlanRevisionListView,
    RestorePlanRevisionView,
)

urlpatterns = [
    path('list/', GoalListView.as_view(), name="goal-list"),
    path('plans/', PlanListView.as_view(), name="plan-list"),
    path('plans/<uuid:plan_id>/', PlanDetailView.as_view(), name="plan-detail"),
    path('plans/<uuid:plan_id>/upgrade/', UpgradeLegacyPlanView.as_view(), name='plan-upgrade'),
    path('plans/<uuid:plan_id>/context/', PlanContextView.as_view(), name="plan-context"),
    path('plans/<uuid:plan_id>/tasks/propose/', TaskPlanProposalView.as_view(), name="plan-tasks-propose"),
    path('plans/<uuid:plan_id>/replan/', ReplanProposalView.as_view(), name="plan-replan"),
    path('plans/<uuid:plan_id>/questions/<uuid:question_id>/answer/', QuestionAnswerView.as_view(), name="question-answer"),
    path(
        'plans/<uuid:plan_id>/changes/',
        PlanChangeListView.as_view(),
        name="plan-change-list",
    ),
    path(
        'plans/<uuid:plan_id>/revisions/',
        PlanRevisionListView.as_view(),
        name="plan-revision-list",
    ),
    path(
        'plans/<uuid:plan_id>/revisions/<int:version>/restore/',
        RestorePlanRevisionView.as_view(),
        name="restore-plan-revision",
    ),
    path('<uuid:pk>/', DeleteGoalView.as_view(), name='goal-delete'),
]

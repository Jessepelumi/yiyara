from django.urls import path
from .views import (
    DecomposeGoalView,
    DeleteGoalView,
    GoalListView,
    ImportPreviewPlanView,
    PlanChangeListView,
    PlanDetailView,
    PlanListView,
    PlanRevisionListView,
    PreviewDecomposeGoalView,
    RestorePlanRevisionView,
)

urlpatterns = [
    path('decompose/preview/', PreviewDecomposeGoalView.as_view(), name='preview-decompose-goal'),
    path('decompose/', DecomposeGoalView.as_view(), name='decompose-goal'),
    path('list/', GoalListView.as_view(), name="goal-list"),
    path('plans/', PlanListView.as_view(), name="plan-list"),
    path(
        'plans/import-preview/',
        ImportPreviewPlanView.as_view(),
        name="import-preview-plan",
    ),
    path('plans/<uuid:plan_id>/', PlanDetailView.as_view(), name="plan-detail"),
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

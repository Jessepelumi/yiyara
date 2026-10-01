from django.urls import path

from .views import ApplyPlanChangeView, PlanMessagesView, RejectPlanChangeView

urlpatterns = [
    path(
        "plans/<uuid:plan_id>/messages/",
        PlanMessagesView.as_view(),
        name="plan-messages",
    ),
    path(
        "plans/<uuid:plan_id>/changes/<uuid:change_id>/apply/",
        ApplyPlanChangeView.as_view(),
        name="apply-plan-change",
    ),
    path(
        "plans/<uuid:plan_id>/changes/<uuid:change_id>/reject/",
        RejectPlanChangeView.as_view(),
        name="reject-plan-change",
    ),
]

from django.urls import path
from .session_views import (SessionApproveView, SessionDetailView, SessionRejectView, FinishSessionView,
    RescheduleSessionView, CancelSessionView, SessionChangeDetailView, SessionChangeApproveView, SessionChangeRejectView)

urlpatterns = [
    path('<uuid:session_id>/', SessionDetailView.as_view(), name='session-detail'),
    path('<uuid:session_id>/approve/', SessionApproveView.as_view(), name='session-approve'),
    path('<uuid:session_id>/reject/', SessionRejectView.as_view(), name='session-reject'),
    path('<uuid:session_id>/finish/', FinishSessionView.as_view(), name='session-finish'),
    path('<uuid:session_id>/reschedule/', RescheduleSessionView.as_view(), name='session-reschedule'),
    path('<uuid:session_id>/cancel/', CancelSessionView.as_view(), name='session-cancel'),
    path('changes/<uuid:change_id>/', SessionChangeDetailView.as_view(), name='session-change-detail'),
    path('changes/<uuid:change_id>/approve/', SessionChangeApproveView.as_view(), name='session-change-approve'),
    path('changes/<uuid:change_id>/reject/', SessionChangeRejectView.as_view(), name='session-change-reject'),
]

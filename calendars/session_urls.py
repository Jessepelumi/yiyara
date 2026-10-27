from django.urls import path
from .session_views import SessionApproveView, SessionDetailView, SessionRejectView

urlpatterns = [
    path('<uuid:session_id>/', SessionDetailView.as_view(), name='session-detail'),
    path('<uuid:session_id>/approve/', SessionApproveView.as_view(), name='session-approve'),
    path('<uuid:session_id>/reject/', SessionRejectView.as_view(), name='session-reject'),
]

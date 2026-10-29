from django.urls import path
from .views import TaskCreateView, TaskDetailView, TaskOutcomeView
from calendars.session_views import ProposeSessionView

urlpatterns = [
    path('', TaskCreateView.as_view(), name='task-create'),
    path('<uuid:task_id>/', TaskDetailView.as_view(), name='task-detail'),
    path('<uuid:task_id>/outcomes/', TaskOutcomeView.as_view(), name='task-outcomes'),
    path('<uuid:task_id>/sessions/propose/', ProposeSessionView.as_view(), name='session-propose'),
]

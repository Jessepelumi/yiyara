from django.contrib import admin
from django.urls import path, include
from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/users/", include("users.urls")),
    path("api/", include("goals.urls")),
    path('api/conversations/', include('conversations.urls')),
    path('test/', views.test_view, name='test'),
]

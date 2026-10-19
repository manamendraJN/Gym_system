from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("settings/gym/", views.GymSettingsView.as_view(), name="gym_settings"),
    path("audit/", views.AuditLogView.as_view(), name="audit_log"),
    path("healthz/", views.healthz, name="healthz"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path("notifications/read/", views.NotificationReadView.as_view(), name="notifications_read_all"),
    path("notifications/<int:pk>/read/", views.NotificationReadView.as_view(), name="notification_read"),
]

from django.urls import path

from . import appearance_views, views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("settings/gym/", views.GymSettingsView.as_view(), name="gym_settings"),
    path("settings/appearance/", appearance_views.AppearanceView.as_view(), name="appearance"),
    path("settings/appearance/preview/", appearance_views.AppearancePreviewView.as_view(), name="appearance_preview"),
    path("settings/appearance/reset/", appearance_views.AppearanceResetView.as_view(), name="appearance_reset"),
    path("audit/", views.AuditLogView.as_view(), name="audit_log"),
    path("healthz/", views.healthz, name="healthz"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path("notifications/read/", views.NotificationReadView.as_view(), name="notifications_read_all"),
    path("notifications/<int:pk>/read/", views.NotificationReadView.as_view(), name="notification_read"),
]

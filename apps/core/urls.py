from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path("notifications/read/", views.NotificationReadView.as_view(), name="notifications_read_all"),
    path("notifications/<int:pk>/read/", views.NotificationReadView.as_view(), name="notification_read"),
]

from django.urls import path

from . import views

app_name = "announcements"

urlpatterns = [
    path("", views.AnnouncementListView.as_view(), name="list"),
    path("new/", views.AnnouncementCreateView.as_view(), name="create"),
    path("<int:pk>/edit/", views.AnnouncementUpdateView.as_view(), name="edit"),
    path("<int:pk>/toggle/", views.AnnouncementToggleView.as_view(), name="toggle"),
]

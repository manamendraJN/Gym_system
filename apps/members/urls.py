from django.urls import path

from . import views

app_name = "members"

urlpatterns = [
    path("", views.MemberListView.as_view(), name="list"),
    path("new/", views.MemberCreateView.as_view(), name="create"),
    path("<int:pk>/", views.MemberDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.MemberUpdateView.as_view(), name="edit"),
    path("<int:pk>/toggle-active/", views.MemberToggleActiveView.as_view(), name="toggle_active"),
    path("<int:pk>/portal-access/", views.MemberPortalAccessView.as_view(), name="portal_access"),
]

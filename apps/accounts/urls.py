from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.GymLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),  # POST only
    path("password/change/", views.GymPasswordChangeView.as_view(), name="password_change"),
    path("password/reset/", views.GymPasswordResetView.as_view(), name="password_reset"),
    path("password/reset/sent/", views.GymPasswordResetDoneView.as_view(), name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/", views.GymPasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("password/reset/complete/", views.GymPasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/new/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", views.UserUpdateView.as_view(), name="user_edit"),
    path("users/<int:pk>/toggle-active/", views.UserToggleActiveView.as_view(), name="user_toggle_active"),
    path("permissions/", views.RolePermissionsView.as_view(), name="permissions"),
]

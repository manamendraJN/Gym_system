from django.urls import path

from . import views

app_name = "workouts"

urlpatterns = [
    path("exercises/", views.ExerciseListView.as_view(), name="exercise_list"),
    path("exercises/new/", views.ExerciseCreateView.as_view(), name="exercise_create"),
    path("exercises/<int:pk>/", views.ExerciseDetailView.as_view(), name="exercise_detail"),
    path("exercises/<int:pk>/edit/", views.ExerciseUpdateView.as_view(), name="exercise_edit"),
    path("plans/", views.PlanListView.as_view(), name="plan_list"),
    path("plans/new/", views.PlanCreateView.as_view(), name="plan_create"),
    path("plans/<int:pk>/", views.PlanDetailView.as_view(), name="plan_detail"),
    path("plans/<int:pk>/edit/", views.PlanUpdateView.as_view(), name="plan_edit"),
    path("plans/<int:pk>/status/", views.PlanStatusView.as_view(), name="plan_status"),
    path("plans/<int:pk>/copy/", views.PlanCopyView.as_view(), name="plan_copy"),
    path("plans/<int:pk>/log/", views.LogWorkoutView.as_view(), name="plan_log"),
    path("plans/<int:pk>/exercises/add/", views.ItemCreateView.as_view(), name="item_add"),
    path("items/<int:pk>/edit/", views.ItemUpdateView.as_view(), name="item_edit"),
    path("items/<int:pk>/remove/", views.ItemRemoveView.as_view(), name="item_remove"),
]

from django.urls import path

from . import views

app_name = "nutrition"

urlpatterns = [
    path("plans/", views.PlanListView.as_view(), name="plan_list"),
    path("plans/new/", views.PlanCreateView.as_view(), name="plan_create"),
    path("plans/<int:pk>/", views.PlanDetailView.as_view(), name="plan_detail"),
    path("plans/<int:pk>/edit/", views.PlanUpdateView.as_view(), name="plan_edit"),
    path("plans/<int:pk>/meals/add/", views.ItemCreateView.as_view(), name="item_add"),
    path("meals/<int:pk>/delete/", views.ItemDeleteView.as_view(), name="item_delete"),
    path("members/<int:member_pk>/profile/", views.ProfileView.as_view(), name="profile"),
]

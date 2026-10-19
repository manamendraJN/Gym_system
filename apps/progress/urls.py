from django.urls import path

from . import views

app_name = "progress"

urlpatterns = [
    path("", views.MemberProgressListView.as_view(), name="list"),
    path("members/<int:member_pk>/", views.MemberProgressView.as_view(), name="member"),
    path("types/", views.TypeListView.as_view(), name="types"),
    path("types/new/", views.TypeCreateView.as_view(), name="type_create"),
    path("types/<int:pk>/edit/", views.TypeUpdateView.as_view(), name="type_edit"),
]

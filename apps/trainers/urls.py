from django.urls import path

from . import views

app_name = "trainers"

urlpatterns = [
    path("", views.TrainerListView.as_view(), name="list"),
    path("new/", views.TrainerCreateView.as_view(), name="create"),
    path("my-members/", views.MyMembersView.as_view(), name="my_members"),
    path("assign/", views.AssignView.as_view(), name="assign"),
    path("assignments/<int:pk>/end/", views.EndAssignmentView.as_view(), name="end_assignment"),
    path("<int:pk>/", views.TrainerDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.TrainerUpdateView.as_view(), name="edit"),
    path("<int:pk>/availability/add/", views.AvailabilityCreateView.as_view(), name="availability_add"),
    path("availability/<int:pk>/delete/", views.AvailabilityDeleteView.as_view(), name="availability_delete"),
    path("<int:pk>/time-off/add/", views.TimeOffCreateView.as_view(), name="timeoff_add"),
    path("time-off/<int:pk>/delete/", views.TimeOffDeleteView.as_view(), name="timeoff_delete"),
    path("<int:pk>/certificates/add/", views.CertificateCreateView.as_view(), name="certificate_add"),
    path("certificates/<int:pk>/download/", views.CertificateDownloadView.as_view(), name="certificate_download"),
    path("certificates/<int:pk>/delete/", views.CertificateDeleteView.as_view(), name="certificate_delete"),
    path("<int:pk>/compensation/add/", views.CompensationCreateView.as_view(), name="compensation_add"),
]

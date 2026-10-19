from django.urls import path

from . import views

app_name = "equipment"

urlpatterns = [
    path("", views.EquipmentListView.as_view(), name="list"),
    path("new/", views.EquipmentCreateView.as_view(), name="create"),
    path("maintenance/", views.MaintenanceListView.as_view(), name="maintenance_list"),
    path("<int:pk>/", views.EquipmentDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.EquipmentUpdateView.as_view(), name="edit"),
    path("<int:pk>/status/", views.StatusView.as_view(), name="status"),
    path("<int:pk>/fault/", views.FaultCreateView.as_view(), name="fault"),
    path("<int:pk>/maintenance/add/", views.MaintenanceCreateView.as_view(), name="maintenance_add"),
]

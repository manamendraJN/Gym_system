from django.urls import path

from . import views

app_name = "suppliers"

urlpatterns = [
    path("", views.SupplierListView.as_view(), name="list"),
    path("new/", views.SupplierCreateView.as_view(), name="create"),
    path("<int:pk>/edit/", views.SupplierUpdateView.as_view(), name="edit"),
]

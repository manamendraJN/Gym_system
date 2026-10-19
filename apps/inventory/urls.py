from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.ItemListView.as_view(), name="list"),
    path("new/", views.ItemCreateView.as_view(), name="create"),
    path("movements/", views.MovementListView.as_view(), name="movements"),
    path("<int:pk>/", views.ItemDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.ItemUpdateView.as_view(), name="edit"),
    path("<int:pk>/purchase/", views.PurchaseView.as_view(), name="purchase"),
    path("<int:pk>/usage/", views.UsageView.as_view(), name="usage"),
    path("<int:pk>/adjust/", views.AdjustView.as_view(), name="adjust"),
]

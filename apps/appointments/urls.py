from django.urls import path

from . import views

app_name = "appointments"

urlpatterns = [
    path("", views.AppointmentListView.as_view(), name="list"),
    path("book/", views.AppointmentBookView.as_view(), name="book"),
    path("slots/", views.SlotsView.as_view(), name="slots"),
    path("<int:pk>/", views.AppointmentDetailView.as_view(), name="detail"),
    path("<int:pk>/reschedule/", views.RescheduleView.as_view(), name="reschedule"),
    path("<int:pk>/cancel/", views.CancelView.as_view(), name="cancel"),
    path("<int:pk>/complete/", views.CompleteView.as_view(), name="complete"),
    path("<int:pk>/no-show/", views.NoShowView.as_view(), name="no_show"),
]

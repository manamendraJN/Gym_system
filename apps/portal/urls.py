from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("trainers/", views.TrainersView.as_view(), name="trainers"),
    path("workouts/", views.WorkoutsView.as_view(), name="workouts"),
    path("workouts/<int:pk>/", views.WorkoutDetailView.as_view(), name="workout"),
    path("workouts/<int:pk>/log/", views.LogWorkoutView.as_view(), name="workout_log"),
    path("appointments/", views.AppointmentsView.as_view(), name="appointments"),
    path("appointments/book/", views.BookView.as_view(), name="book"),
    path("appointments/slots/", views.SlotsView.as_view(), name="slots"),
    path("appointments/<int:pk>/reschedule/", views.RescheduleView.as_view(), name="reschedule"),
    path("appointments/<int:pk>/cancel/", views.CancelView.as_view(), name="cancel"),
    path("payments/", views.PaymentsView.as_view(), name="payments"),
    path("payments/<int:pk>/receipt/", views.ReceiptView.as_view(), name="receipt"),
    path("payments/<int:pk>/receipt.pdf", views.ReceiptPDFView.as_view(), name="receipt_pdf"),
    path("progress/", views.ProgressView.as_view(), name="progress"),
    path("nutrition/", views.NutritionView.as_view(), name="nutrition"),
]

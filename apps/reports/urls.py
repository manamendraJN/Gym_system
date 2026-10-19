from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportIndexView.as_view(), name="index"),
    path("<slug:key>/", views.ReportView.as_view(), name="report"),
]

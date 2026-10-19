from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("settings/", views.SettingsView.as_view(), name="settings"),
    path("preferences/", views.PreferencesView.as_view(), name="preferences"),
    path("webhooks/twilio/", views.TwilioStatusWebhook.as_view(), name="twilio_webhook"),
]

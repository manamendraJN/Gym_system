from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import FormView, UpdateView

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog

from . import delivery
from .models import DeliveryStatus, NotificationDelivery, NotificationPreference, NotificationSettings, parse_days


class SettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = NotificationSettings
        fields = ["membership_expiry_days", "payment_due_grace_days", "payment_due_offsets", "appointment_reminder_hours",
                  "maintenance_reminder_days", "email_enabled", "sms_enabled", "whatsapp_enabled"]

    def _days(self, name, negative=False):
        try:
            return ", ".join(str(d) for d in parse_days(self.cleaned_data[name], allow_negative=negative))
        except ValueError as exc:
            raise forms.ValidationError(f"Enter whole numbers separated by commas ({exc}).")

    def clean_membership_expiry_days(self):
        return self._days("membership_expiry_days")

    def clean_maintenance_reminder_days(self):
        return self._days("maintenance_reminder_days")

    def clean_payment_due_offsets(self):
        return self._days("payment_due_offsets", negative=True)

    def clean_appointment_reminder_hours(self):
        hours = self.cleaned_data["appointment_reminder_hours"]
        if not 1 <= hours <= 168:
            raise forms.ValidationError("Use between 1 and 168 hours.")
        return hours


class SettingsView(GymPermissionMixin, FormView):
    required_permission = "notifications.configure"
    form_class = SettingsForm
    template_name = "notifications/settings.html"
    success_url = reverse_lazy("notifications:settings")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": NotificationSettings.load()}

    def get_context_data(self, **kwargs):
        configured = delivery.configured_channels()
        counts = {s: NotificationDelivery.objects.filter(status=s).count() for s in DeliveryStatus.values}
        return super().get_context_data(channels=[
            {"label": "E-mail", "ok": configured["email"], "hint": "Needs EMAIL_BACKEND (SMTP) and EMAIL_HOST in .env."},
            {"label": "SMS", "ok": configured["sms"], "hint": "Needs SMS_PROVIDER=twilio, TWILIO_ACCOUNT_SID/AUTH_TOKEN and TWILIO_SMS_FROM."},
            {"label": "WhatsApp", "ok": configured["whatsapp"], "hint": "Needs WHATSAPP_PROVIDER=twilio, TWILIO credentials and TWILIO_WHATSAPP_FROM."},
        ], counts=counts, confirmations=bool(settings.TWILIO_AUTH_TOKEN), **kwargs)

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.updated_by = self.request.user
        obj.save()
        ActivityLog.record(self.request.user, "notification_settings", "Updated notification settings")
        messages.success(self.request, "Notification settings saved.")
        return super().form_valid(form)


class PreferencesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = NotificationPreference
        fields = ["email_enabled", "sms_enabled", "whatsapp_enabled"]


class PreferencesView(FormView):
    """Every signed-in user chooses which external copies they want (in-app notifications are always on)."""

    form_class = PreferencesForm
    template_name = "notifications/preferences.html"
    success_url = reverse_lazy("notifications:preferences")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": delivery.preference_for(self.request.user)}

    def get_context_data(self, **kwargs):
        configured = delivery.configured_channels()
        return super().get_context_data(configured=configured, phone=delivery.phone_for(self.request.user), **kwargs)

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "Your notification preferences were saved.")
        return super().form_valid(form)


@method_decorator([csrf_exempt, login_not_required], name="dispatch")
class TwilioStatusWebhook(View):
    """Delivery confirmations from Twilio. Requests are accepted only with a valid X-Twilio-Signature (HMAC of the URL and
    parameters using the account's auth token); without a token configured the endpoint does not exist."""

    http_method_names = ["post"]

    def post(self, request):
        token = settings.TWILIO_AUTH_TOKEN
        if not token:
            raise Http404
        url = settings.TWILIO_WEBHOOK_URL or request.build_absolute_uri()
        params = {k: v for k, v in request.POST.items()}
        if not delivery.valid_twilio_signature(url, params, request.headers.get("X-Twilio-Signature", ""), token):
            return HttpResponseForbidden("Invalid signature")
        delivery.apply_twilio_status(params.get("MessageSid", ""), params.get("MessageStatus", ""))
        return HttpResponse(status=204)

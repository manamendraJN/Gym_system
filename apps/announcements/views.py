from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog

from . import services
from .models import Announcement, AnnouncementKind, Audience

DT = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class AnnouncementForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ["title", "kind", "audience", "body", "publish_at", "expires_at", "send_notification", "is_active"]
        widgets = {"publish_at": DT, "expires_at": DT, "body": forms.Textarea(attrs={"rows": 5})}
        labels = {"is_active": "Active"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("publish_at", "expires_at"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        if self.instance.pk and self.instance.notified_at:
            self.fields["send_notification"].disabled = True
            self.fields["send_notification"].help_text = "Notifications were already sent; editing does not send them again."

    def clean(self):
        c = super().clean()
        if c.get("publish_at") and c.get("expires_at") and c["expires_at"] <= c["publish_at"]:
            self.add_error("expires_at", "The expiry must be after the publication time.")
        return c


class AnnouncementListView(GymPermissionMixin, ListView):
    required_permission = "announcements.manage"
    template_name = "announcements/announcement_list.html"
    paginate_by = 20

    def get_queryset(self):
        qs = Announcement.objects.select_related("created_by")
        status = self.request.GET.get("status", "")
        now = timezone.now()
        if status == "live":
            qs = services.live_announcements(now)
        elif status == "scheduled":
            qs = qs.filter(is_active=True, publish_at__gt=now)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(status=self.request.GET.get("status", ""), **kwargs)


class AnnouncementCreateView(GymPermissionMixin, CreateView):
    required_permission = "announcements.manage"
    model = Announcement
    form_class = AnnouncementForm
    template_name = "announcements/announcement_form.html"
    success_url = reverse_lazy("announcements:list")

    def get_initial(self):
        return {"publish_at": timezone.localtime().replace(second=0, microsecond=0)}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New announcement", **kwargs)

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        sent = services.publish_due()  # live ones notify immediately; scheduled ones wait for the scheduler
        ActivityLog.record(self.request.user, "announcement_created", f"Created announcement “{self.object.title}”")
        messages.success(self.request, "Announcement saved." + (f" {sent} notification(s) created." if sent else ""))
        return response


class AnnouncementUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "announcements.manage"
    model = Announcement
    form_class = AnnouncementForm
    template_name = "announcements/announcement_form.html"
    success_url = reverse_lazy("announcements:list")

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.title}", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        services.publish_due()
        ActivityLog.record(self.request.user, "announcement_updated", f"Updated announcement “{self.object.title}”")
        messages.success(self.request, "Announcement updated.")
        return response


class AnnouncementToggleView(GymPermissionMixin, View):
    required_permission = "announcements.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        ann = get_object_or_404(Announcement, pk=pk)
        ann.is_active = not ann.is_active
        ann.save(update_fields=["is_active", "updated_at"])
        word = "activated" if ann.is_active else "deactivated"
        ActivityLog.record(request.user, f"announcement_{word}", f"{word.capitalize()} announcement “{ann.title}”")
        messages.success(request, f"Announcement {word}.")
        return redirect("announcements:list")

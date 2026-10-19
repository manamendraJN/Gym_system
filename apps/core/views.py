from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import FormView, ListView, TemplateView

from django import forms
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse
from django.contrib.auth.decorators import login_not_required
from django.utils.dateparse import parse_date

from apps.accounts.forms import StyledFormMixin
from apps.accounts.mixins import GymPermissionMixin
from apps.accounts.models import User
from apps.core.gym import gym

from .models import ActivityLog, GymSettings, Notification

from .stats import get_dashboard_stats


class HomeView(TemplateView):
    """Dashboard for roles holding dashboard.view; everyone else (members) gets their own account page."""

    def get(self, request, *args, **kwargs):
        if request.user.role == "member":  # members live in the portal
            return redirect("portal:home")
        return super().get(request, *args, **kwargs)

    def get_template_names(self):
        if self.request.user.has_gym_perm("dashboard.view"):
            return ["core/dashboard.html"]
        return ["core/portal.html"]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if self.request.user.has_gym_perm("dashboard.view"):
            ctx.update(get_dashboard_stats(user=self.request.user))
        return ctx


def permission_denied(request, exception=None):
    return render(request, "errors/403.html", status=403)


def page_not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def server_error(request):
    # Deliberately generic: no exception details are exposed.
    return render(request, "errors/500.html", status=500)


class NotificationListView(ListView):
    """A user's own notifications (everyone signed in; queries are always filtered on the current user)."""

    template_name = "core/notifications.html"
    paginate_by = 20

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user).prefetch_related("deliveries")
        if self.request.GET.get("unread"):
            qs = qs.filter(read_at__isnull=True)
        kind = self.request.GET.get("kind", "")
        if kind:
            qs = qs.filter(kind=kind)
        return qs

    def get_context_data(self, **kwargs):
        kinds = Notification.objects.filter(user=self.request.user).values_list("kind", flat=True).distinct().order_by("kind")
        return super().get_context_data(kinds=list(kinds), kind=self.request.GET.get("kind", ""), unread_only=bool(self.request.GET.get("unread")), **kwargs)


class NotificationReadView(View):
    http_method_names = ["post"]

    def post(self, request, pk=None):
        qs = Notification.objects.filter(user=request.user, read_at__isnull=True)
        if pk is not None:
            note = get_object_or_404(Notification, pk=pk, user=request.user)
            qs = qs.filter(pk=note.pk)
        qs.update(read_at=timezone.now())
        target = request.POST.get("next", "")
        if pk is not None and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) and target:
            return redirect(target)
        return redirect("core:notifications")


# ----------------------------------------------------------------------------------- gym settings
class GymSettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = GymSettings
        fields = ["name", "tagline", "address", "phone", "email", "receipt_footer"]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Enter the gym's name.")
        return name


class GymSettingsView(GymPermissionMixin, FormView):
    required_permission = "settings.manage"
    form_class = GymSettingsForm
    template_name = "core/gym_settings.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Gym settings", **kwargs)

    def get_form_kwargs(self):
        current = gym()
        instance = current if isinstance(current, GymSettings) else GymSettings(pk=1, name=current.name)
        return {**super().get_form_kwargs(), "instance": instance}

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.pk, obj.updated_by = 1, self.request.user
        obj.save()
        ActivityLog.record(self.request.user, "gym_settings_updated", f"Updated gym settings (name: {obj.name})")
        messages.success(self.request, "Gym settings saved.")
        return redirect("core:gym_settings")


# ------------------------------------------------------------------------------------- audit log
class AuditLogView(GymPermissionMixin, ListView):
    """Read-only view of the activity trail (sign-ins, user/permission changes, financial and operational actions)."""

    required_permission = "audit.view"
    template_name = "core/audit_log.html"
    paginate_by = 50

    def get_queryset(self):
        g = self.request.GET
        qs = ActivityLog.objects.select_related("user")
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(Q(description__icontains=q) | Q(action__icontains=q) | Q(user__username__icontains=q))
        if g.get("user", "").isdigit():
            qs = qs.filter(user_id=g["user"])
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(created_at__date__gte=d_from)
        if d_to:
            qs = qs.filter(created_at__date__lte=d_to)
        area = g.get("area", "")
        if area in AREAS:
            qs = qs.filter(action__startswith=AREAS[area])
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        return super().get_context_data(
            q=g.get("q", ""), user_id=g.get("user", ""), date_from=g.get("from", ""), date_to=g.get("to", ""), area=g.get("area", ""),
            areas=list(AREAS), users=User.objects.order_by("username"), filtered_on=any(g.get(k) for k in ("q", "user", "from", "to", "area")), **kwargs)


AREAS = {"sign-in": "log", "users": "user_", "members": "member", "memberships": "membership_", "payments": "payment_", "expenses": "expense_",
         "equipment": "equipment_", "inventory": "inventory_", "announcements": "announcement_", "permissions": "permissions_"}


@login_not_required
def healthz(request):
    """Liveness / readiness probe for load balancers: confirms the app and its database respond. Reveals nothing else."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

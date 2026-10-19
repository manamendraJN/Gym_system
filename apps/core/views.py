from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import ListView, TemplateView

from .models import Notification

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

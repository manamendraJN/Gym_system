from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from django.conf import settings

from apps.accounts.mixins import GymPermissionMixin
from apps.core.listing import resolve_sort, sort_columns
from apps.core.models import ActivityLog
from apps.members.models import Member

from . import forms, services
from .models import Membership, MembershipPlan, MembershipStatus, STATUS_LABELS
from .queries import expired_memberships, expiring_soon
from .services import MembershipError


# ------------------------------------------------------------------------------------------ plans
class PlanListView(GymPermissionMixin, ListView):
    required_permission = "plans.view"
    template_name = "memberships/plan_list.html"
    paginate_by = 12

    def get_queryset(self):
        qs = MembershipPlan.objects.annotate(
            live_count=Count("memberships", filter=Q(memberships__status__in=["active", "suspended"],
                                                     memberships__end_date__gte=timezone.localdate()))
        )
        q = self.request.GET.get("q", "").strip()
        qs = qs.order_by("price", "name", "pk")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        status = self.request.GET.get("status", "")
        if status in ("active", "inactive"):
            qs = qs.filter(is_active=(status == "active"))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(q=self.request.GET.get("q", ""), status=self.request.GET.get("status", ""))
        return ctx


class PlanCreateView(GymPermissionMixin, CreateView):
    # Creating a plan sets its price, so both permissions are required.
    required_permission = "plans.manage"
    model = MembershipPlan
    form_class = forms.PlanForm
    template_name = "memberships/plan_form.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.has_gym_perm("plans.price"):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "can_set_price": True}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New membership plan", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "plan_created", f"Created plan “{self.object.name}” at LKR {self.object.price:,.2f}")
        messages.success(self.request, f"Plan “{self.object.name}” was created.")
        return response


class PlanUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "plans.manage"
    model = MembershipPlan
    form_class = forms.PlanForm
    template_name = "memberships/plan_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "can_set_price": self.request.user.has_gym_perm("plans.price")}

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

    def form_valid(self, form):
        old_price = MembershipPlan.objects.values_list("price", flat=True).get(pk=self.object.pk)
        response = super().form_valid(form)
        if self.object.price != old_price:
            ActivityLog.record(self.request.user, "plan_price_changed",
                               f"Changed price of “{self.object.name}” from LKR {old_price:,.2f} to LKR {self.object.price:,.2f}")
        ActivityLog.record(self.request.user, "plan_updated", f"Updated plan “{self.object.name}”")
        messages.success(self.request, "The plan was updated. Existing memberships keep the price they were bought at.")
        return response


class PlanDetailView(GymPermissionMixin, DetailView):
    required_permission = "plans.view"
    model = MembershipPlan
    template_name = "memberships/plan_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        ms = self.object.memberships
        ctx["live_count"] = ms.filter(status__in=["active", "suspended"], end_date__gte=today).count()
        ctx["total_count"] = ms.count()
        return ctx


class PlanToggleActiveView(GymPermissionMixin, View):
    """Deactivating a plan stops new assignments only; existing memberships are unaffected."""

    required_permission = "plans.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        plan = get_object_or_404(MembershipPlan, pk=pk)
        plan.is_active = not plan.is_active
        plan.save(update_fields=["is_active", "updated_at"])
        word = "activated" if plan.is_active else "deactivated"
        ActivityLog.record(request.user, f"plan_{word}", f"{word.capitalize()} plan “{plan.name}”")
        messages.success(request, f"Plan “{plan.name}” was {word}.")
        return redirect(plan)


# ------------------------------------------------------------------------------------ membership lists
STATUS_FILTERS = {k: STATUS_LABELS[k] for k in ("active", "scheduled", "expired", "suspended", "cancelled", "superseded")}
SORTS = {"member": "member__full_name", "plan": "plan__name", "start": "start_date", "end": "end_date", "price": "price"}
COLUMNS = [("member", "Member"), ("plan", "Plan"), ("start", "Start"), ("end", "End"), ("price", "Price")]


def _status_q(key, today):
    A = MembershipStatus.ACTIVE
    return {
        "active": Q(status=A, start_date__lte=today, end_date__gte=today),
        "scheduled": Q(status=A, start_date__gt=today),
        "expired": Q(status=A, end_date__lt=today),
    }.get(key) or Q(status=key)


def _search(qs, q):
    return qs.filter(Q(member__full_name__icontains=q) | Q(member__member_id__icontains=q) | Q(member__phone__contains=q)) if q else qs


class MembershipListView(GymPermissionMixin, ListView):
    required_permission = "memberships.view"
    template_name = "memberships/membership_list.html"
    paginate_by = 15

    def get_queryset(self):
        g, today = self.request.GET, timezone.localdate()
        qs = _search(Membership.objects.select_related("member", "plan"), g.get("q", "").strip())
        if g.get("status") in STATUS_FILTERS:
            qs = qs.filter(_status_q(g["status"], today))
        if g.get("plan", "").isdigit():
            qs = qs.filter(plan_id=g["plan"])
        self.sort, self.direction, order = resolve_sort(self.request, SORTS, "start")
        if "sort" not in g:
            self.direction, order = "desc", ["-start_date", "-pk"]
        return qs.order_by(*order)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        ctx.update(
            q=g.get("q", ""), status=g.get("status", ""), plan=g.get("plan", ""),
            statuses=STATUS_FILTERS.items(), plans=MembershipPlan.objects.all(),
            columns={c["key"]: c for c in sort_columns(self.request, COLUMNS, self.sort, self.direction)},
        )
        return ctx


class ExpiredListView(GymPermissionMixin, ListView):
    required_permission = "memberships.view"
    template_name = "memberships/expired_list.html"
    paginate_by = 15

    def get_queryset(self):
        return _search(expired_memberships(), self.request.GET.get("q", "").strip()).order_by("-end_date", "-pk")

    def get_context_data(self, **kwargs):
        return super().get_context_data(q=self.request.GET.get("q", ""), **kwargs)


class ExpiringSoonView(GymPermissionMixin, ListView):
    required_permission = "memberships.view"
    template_name = "memberships/expiring_list.html"
    paginate_by = 15
    day_options = (7, 14, 30)

    @cached_property
    def days(self):
        raw = self.request.GET.get("days", "")
        return int(raw) if raw.isdigit() and int(raw) in self.day_options else settings.MEMBERSHIP_EXPIRING_SOON_DAYS

    def get_queryset(self):
        return _search(expiring_soon(self.days), self.request.GET.get("q", "").strip()).order_by("end_date", "pk")

    def get_context_data(self, **kwargs):
        days_options = sorted(set(self.day_options) | {self.days})
        return super().get_context_data(q=self.request.GET.get("q", ""), days=self.days, day_options=days_options, **kwargs)


class MembershipDetailView(GymPermissionMixin, DetailView):
    required_permission = "memberships.view"
    model = Membership
    template_name = "memberships/membership_detail.html"

    def get_queryset(self):
        return Membership.objects.select_related("member", "plan", "previous", "created_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.object
        ctx["history"] = Membership.objects.filter(member=m.member).select_related("plan")
        ctx["followed_by"] = m.followed_by.first()
        user = self.request.user
        if user.has_gym_perm("payments.view"):
            from apps.payments.billing import membership_billing

            ctx["billing"] = membership_billing(m)
            ctx["payments"] = m.payments.select_related("recorded_by")
        key = m.status_key
        ctx["can_renew"] = key in ("active", "expired") and m.member.is_active and not m.followed_by.filter(status__in=["active", "suspended"]).exists()
        ctx["can_change"] = key == "active" and m.member.is_active
        ctx["can_suspend"] = key == "active"
        ctx["can_reactivate"] = key == "suspended"
        ctx["can_cancel"] = key in ("active", "scheduled", "suspended") and not m.followed_by.filter(status__in=["active", "suspended"]).exists()
        return ctx


# ---------------------------------------------------------------------------------- membership actions
class _ActionBase(GymPermissionMixin, FormView):
    """Shared plumbing: load the target, run a service call, show rule violations as form errors."""

    required_permission = "memberships.manage"
    template_name = "memberships/action_form.html"
    title = ""
    submit_label = "Save"
    danger = False
    intro = ""

    def perform(self, form):
        raise NotImplementedError

    def form_valid(self, form):
        try:
            message, target = self.perform(form)
        except MembershipError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, message)
        return redirect(target)

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            title=self.title, submit_label=self.submit_label, danger=self.danger, intro=self.intro,
            member=self.member, membership=getattr(self, "membership", None), **kwargs,
        )


class AssignMembershipView(_ActionBase):
    form_class = forms.AssignForm
    title = "Assign membership"
    submit_label = "Assign membership"

    @cached_property
    def member(self):
        return get_object_or_404(Member, pk=self.kwargs["member_pk"])

    def perform(self, form):
        m = services.assign_membership(self.member, form.cleaned_data["plan"], form.cleaned_data["start_date"],
                                       self.request.user, form.cleaned_data["notes"])
        ActivityLog.record(self.request.user, "membership_assigned", f"Assigned {m.plan.name} to {self.member.full_name}")
        return f"{m.plan.name} assigned to {self.member.full_name} ({m.start_date:%b %d, %Y} – {m.end_date:%b %d, %Y}).", m


class _ExistingMembershipView(_ActionBase):
    @cached_property
    def membership(self):
        return get_object_or_404(Membership.objects.select_related("member", "plan"), pk=self.kwargs["pk"])

    @property
    def member(self):
        return self.membership.member


class RenewMembershipView(_ExistingMembershipView):
    form_class = forms.PlanChoiceForm
    title = "Renew membership"
    submit_label = "Renew"
    intro = ("If the membership is still running, the renewal starts the day after it ends so no days are lost. "
             "If it has already expired, the renewal starts today. The current period is never modified.")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "current_plan": self.membership.plan}

    def perform(self, form):
        m = services.renew_membership(self.membership, self.request.user, form.cleaned_data["plan"], form.cleaned_data["notes"])
        ActivityLog.record(self.request.user, "membership_renewed", f"Renewed {m.plan.name} for {self.member.full_name}")
        return f"Renewed: {m.start_date:%b %d, %Y} – {m.end_date:%b %d, %Y}.", m


class ChangePlanView(_ExistingMembershipView):
    form_class = forms.PlanChoiceForm
    title = "Upgrade or downgrade plan"
    submit_label = "Change plan"
    intro = ("The current period ends today and a new full period on the selected plan starts today. "
             "No proration is applied; any price difference is settled through payments.")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "current_plan": self.membership.plan, "exclude_current": True}

    def perform(self, form):
        m = services.change_plan(self.membership, form.cleaned_data["plan"], self.request.user, form.cleaned_data["notes"])
        ActivityLog.record(self.request.user, f"membership_{m.kind}", f"{m.get_kind_display()} to {m.plan.name} for {self.member.full_name}")
        return f"{m.get_kind_display()} complete: now on {m.plan.name} until {m.end_date:%b %d, %Y}.", m


class SuspendMembershipView(_ExistingMembershipView):
    form_class = forms.ReasonForm
    title = "Suspend membership"
    submit_label = "Suspend"
    danger = True
    intro = (f"The member loses access immediately. When reactivated, suspended days (up to "
             f"{settings.MEMBERSHIP_MAX_SUSPENSION_DAYS}) are added back to the end date.")

    def perform(self, form):
        m = services.suspend_membership(self.membership, self.request.user, form.cleaned_data["reason"])
        ActivityLog.record(self.request.user, "membership_suspended", f"Suspended membership of {self.member.full_name}")
        return "The membership was suspended.", m


class CancelMembershipView(_ExistingMembershipView):
    required_permission = "memberships.cancel"
    form_class = forms.ReasonForm
    title = "Cancel membership"
    submit_label = "Cancel membership"
    danger = True
    intro = "Access ends immediately and this cannot be undone. The record stays in the member's history. Refunds are handled in payments."

    def perform(self, form):
        m = services.cancel_membership(self.membership, self.request.user, form.cleaned_data["reason"])
        ActivityLog.record(self.request.user, "membership_cancelled", f"Cancelled membership of {self.member.full_name}")
        return "The membership was cancelled.", m


class ReactivateMembershipView(GymPermissionMixin, View):
    required_permission = "memberships.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        membership = get_object_or_404(Membership, pk=pk)
        try:
            m, credited = services.reactivate_membership(membership, request.user)
        except MembershipError as exc:
            messages.error(request, str(exc))
            return redirect(membership)
        ActivityLog.record(request.user, "membership_reactivated", f"Reactivated membership of {m.member.full_name}")
        messages.success(request, f"Membership reactivated; {credited} day(s) added. It now ends {m.end_date:%b %d, %Y}.")
        return redirect(m)

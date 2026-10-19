from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.listing import resolve_sort, sort_columns
from apps.core.models import ActivityLog
from apps.trainers.access import scope_members
from apps.memberships.models import Membership
from apps.memberships.queries import MEMBER_STATUS_LABELS, members_with_status

from .forms import MemberForm
from .portal_access import create_portal_login, link_portal_user, unlink_portal_user
from .models import Member, phone_search_term

SORTS = {"name": "full_name", "member_id": "member_id", "registered": "registration_date", "status": "membership_status"}
COLUMNS = [("member_id", "Member ID"), ("name", "Member"), ("registered", "Registered"), ("status", "Status")]


class MemberListView(GymPermissionMixin, ListView):
    required_permission = "members.view"
    template_name = "members/member_list.html"
    paginate_by = 15

    def get_queryset(self):
        qs = members_with_status(scope_members(self.request.user, Member.objects.all()))
        g = self.request.GET
        q = g.get("q", "").strip()
        if q:
            cond = Q(full_name__icontains=q) | Q(member_id__icontains=q) | Q(email__icontains=q)
            digits = phone_search_term(q)
            if digits:
                cond |= Q(phone__contains=digits)
            qs = qs.filter(cond)
        status = g.get("status", "")
        if status in MEMBER_STATUS_LABELS:
            qs = qs.filter(membership_status=status)
        date_from, date_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if date_from:
            qs = qs.filter(registration_date__gte=date_from)
        if date_to:
            qs = qs.filter(registration_date__lte=date_to)
        self.sort, self.direction, order = resolve_sort(self.request, SORTS, "name")
        return qs.order_by(*order)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        ctx.update(
            q=g.get("q", ""), status=g.get("status", ""), date_from=g.get("from", ""), date_to=g.get("to", ""),
            statuses=MEMBER_STATUS_LABELS.items(),
            columns={c["key"]: c for c in sort_columns(self.request, COLUMNS, self.sort, self.direction)},
            filtered=any(g.get(k) for k in ("q", "status", "from", "to")),
        )
        return ctx


class MemberCreateView(GymPermissionMixin, CreateView):
    required_permission = "members.manage"
    model = Member
    form_class = MemberForm
    template_name = "members/member_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Register member", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "member_created", f"Registered member {self.object.full_name} ({self.object.member_id})")
        messages.success(self.request, f"{self.object.full_name} was registered as {self.object.member_id}.")
        return response


class MemberUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "members.manage"
    model = Member
    form_class = MemberForm
    template_name = "members/member_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.full_name}", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "member_updated", f"Updated member {self.object.full_name} ({self.object.member_id})")
        messages.success(self.request, "Member details were updated.")
        return response


class MemberDetailView(GymPermissionMixin, DetailView):
    required_permission = "members.view"
    model = Member
    template_name = "members/member_detail.html"

    def get_queryset(self):
        # Trainers only see members assigned to them (other staff with members.view see everyone).
        return scope_members(self.request.user, Member.objects.all())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        member = self.object
        ctx["status_key"] = members_with_status(Member.objects.filter(pk=member.pk)).get().membership_status
        ctx["status_label"] = MEMBER_STATUS_LABELS[ctx["status_key"]]
        if user.has_gym_perm("trainers.view"):
            ctx["assigned_trainers"] = list(member.trainer_assignments.select_related("trainer").filter(end_date__isnull=True))
        if user.has_gym_perm("users.manage"):
            from apps.accounts.models import Role, User

            ctx["portal_candidates"] = User.objects.filter(role=Role.MEMBER, is_active=True, member_profile__isnull=True)
        if user.has_gym_perm("attendance.view"):
            ctx["recent_visits"] = member.attendance.select_related("membership__plan")[:5]
            ctx["visit_count"] = member.attendance.count()
        if user.has_gym_perm("payments.view"):
            from apps.payments.billing import with_billing
            from apps.memberships.models import Membership

            billed = with_billing(Membership.objects.filter(member=member).exclude(status="cancelled"))
            ctx["outstanding_total"] = sum((m.outstanding for m in billed), 0)
            ctx["recent_payments"] = member.payments.exclude(kind="adjustment").select_related("membership__plan")[:5]
        if user.has_gym_perm("memberships.view"):
            history = list(member.memberships.select_related("plan"))
            ctx["history"] = history
            ctx["current"] = next((m for m in history if m.status_key in ("active",)), None) or next(
                (m for m in history if m.status_key in ("suspended", "scheduled")), None
            ) or (history[0] if history else None)
        return ctx


class MemberToggleActiveView(GymPermissionMixin, View):
    """Deactivate / reactivate a member. Memberships and every historical record are left untouched."""

    required_permission = "members.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        member = get_object_or_404(Member, pk=pk)
        if member.is_active:
            member.is_active, member.deactivated_at = False, timezone.now()
            word = "deactivated"
        else:
            member.is_active, member.deactivated_at = True, None
            word = "reactivated"
        member.save(update_fields=["is_active", "deactivated_at", "updated_at"])
        ActivityLog.record(request.user, f"member_{word}", f"{word.capitalize()} member {member.full_name} ({member.member_id})")
        messages.success(request, f"{member.full_name} was {word}. All history has been kept.")
        return redirect(member)


class MemberPortalAccessView(GymPermissionMixin, View):
    """Create / link / unlink the member's portal login. Needs users.manage because it creates accounts."""

    required_permission = "users.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        from .portal_access import PortalAccessError

        member = get_object_or_404(Member, pk=pk)
        action = request.POST.get("action")
        try:
            if action == "create":
                create_portal_login(member, request)
                messages.success(request, f"A portal login was created and a password-setup email was sent to {member.email}.")
            elif action == "link":
                link_portal_user(member, request.POST.get("user", ""))
                messages.success(request, "The account was linked.")
            elif action == "unlink":
                unlink_portal_user(member)
                messages.success(request, "The portal login was unlinked (the account itself was not deleted).")
            else:
                messages.error(request, "Unknown action.")
        except PortalAccessError as exc:
            messages.error(request, str(exc))
        else:
            ActivityLog.record(request.user, f"member_portal_{action}", f"Portal access ({action}) for {member.full_name} ({member.member_id})")
        return redirect(member)

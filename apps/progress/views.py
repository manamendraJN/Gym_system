from django.contrib import messages
from django.db.models import OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.functional import cached_property
from django.views.generic import CreateView, FormView, ListView, UpdateView

from apps.accounts.mixins import GymPermissionMixin
from apps.members.models import Member, phone_search_term
from apps.trainers.access import can_access_member, scope_members

from . import forms, services
from .models import MeasurementType, ProgressRecord


class MemberProgressListView(GymPermissionMixin, ListView):
    """Members whose progress the user may open (trainers: assigned members only)."""

    required_permission = "progress.view"
    template_name = "progress/member_list.html"
    paginate_by = 20

    def get_queryset(self):
        latest = ProgressRecord.objects.filter(member=OuterRef("pk")).order_by("-recorded_on", "-id")
        qs = scope_members(self.request.user, Member.objects.filter(is_active=True)).annotate(
            last_recorded=Subquery(latest.values("recorded_on")[:1]), last_weight=Subquery(latest.values("weight_kg")[:1]))
        q = self.request.GET.get("q", "").strip()
        if q:
            cond = Q(full_name__icontains=q) | Q(member_id__icontains=q)
            if phone_search_term(q):
                cond |= Q(phone__contains=phone_search_term(q))
            qs = qs.filter(cond)
        return qs.order_by("full_name", "pk")

    def get_context_data(self, **kwargs):
        return super().get_context_data(q=self.request.GET.get("q", ""), **kwargs)


class MemberProgressView(GymPermissionMixin, FormView):
    """History, charts and (for authorised staff) a form to add a record."""

    required_permission = "progress.view"
    form_class = forms.ProgressForm
    template_name = "progress/member_progress.html"

    @cached_property
    def member(self):
        member = get_object_or_404(Member, pk=self.kwargs["member_pk"])
        if not can_access_member(self.request.user, member):
            raise Http404
        return member

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST" and request.user.is_authenticated and not request.user.has_gym_perm("progress.manage"):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        records = list(self.member.progress_records.prefetch_related("measurements__type"))
        return super().get_context_data(
            member=self.member, records=records, chart=services.chart_data(self.member),
            can_add=self.request.user.has_gym_perm("progress.manage"), portal=False, **kwargs)

    def form_valid(self, form):
        services.add_record(self.member, form, self.request.user, "staff")
        messages.success(self.request, "Progress recorded.")
        return redirect("progress:member", member_pk=self.member.pk)


class TypeListView(GymPermissionMixin, ListView):
    required_permission = "progress.view"
    template_name = "progress/type_list.html"
    model = MeasurementType


class TypeCreateView(GymPermissionMixin, CreateView):
    required_permission = "progress.manage"
    model = MeasurementType
    form_class = forms.MeasurementTypeForm
    template_name = "progress/type_form.html"

    def get_success_url(self):
        return "/progress/types/"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New measurement", **kwargs)


class TypeUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "progress.manage"
    model = MeasurementType
    form_class = forms.MeasurementTypeForm
    template_name = "progress/type_form.html"

    def get_success_url(self):
        return "/progress/types/"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.name}", **kwargs)

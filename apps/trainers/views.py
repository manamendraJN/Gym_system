from django.contrib import messages
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, TemplateView, UpdateView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog
from apps.members.models import Member
from apps.memberships.queries import members_with_status

from . import forms, services
from .access import scope_members, trainer_of
from .models import (
    EmploymentStatus, Trainer, TrainerAssignment, TrainerAvailability, TrainerCertificate, TrainerCompensation,
    TrainerTimeOff,
)
from .services import AssignmentError


class TrainerListView(GymPermissionMixin, ListView):
    required_permission = "trainers.view"
    template_name = "trainers/trainer_list.html"
    paginate_by = 12

    def get_queryset(self):
        today = timezone.localdate()
        qs = Trainer.objects.annotate(
            member_count=Count("assignments", filter=Q(assignments__end_date__isnull=True), distinct=True))
        g = self.request.GET
        if g.get("q", "").strip():
            q = g["q"].strip()
            qs = qs.filter(Q(full_name__icontains=q) | Q(specialties__icontains=q) | Q(email__icontains=q) | Q(phone__contains=q))
        if g.get("status") in EmploymentStatus.values:
            qs = qs.filter(employment_status=g["status"])
        return qs.order_by("full_name", "pk")

    def get_context_data(self, **kwargs):
        g = self.request.GET
        return super().get_context_data(q=g.get("q", ""), status=g.get("status", ""), statuses=EmploymentStatus.choices, **kwargs)


class TrainerCreateView(GymPermissionMixin, CreateView):
    required_permission = "trainers.manage"
    model = Trainer
    form_class = forms.TrainerForm
    template_name = "trainers/trainer_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="New trainer", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "trainer_created", f"Created trainer profile {self.object.full_name}")
        messages.success(self.request, f"Trainer {self.object.full_name} was created. Add their weekly availability next.")
        return response


class TrainerUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "trainers.manage"
    model = Trainer
    form_class = forms.TrainerForm
    template_name = "trainers/trainer_form.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.full_name}", **kwargs)

    def form_valid(self, form):
        was = Trainer.objects.values_list("employment_status", flat=True).get(pk=self.object.pk)
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "trainer_updated", f"Updated trainer {self.object.full_name}")
        if self.object.employment_status == EmploymentStatus.TERMINATED and was != EmploymentStatus.TERMINATED:
            cancelled = services.offboard_trainer(self.object, self.request.user)
            messages.warning(self.request, f"Assignments were ended and {cancelled} upcoming session(s) cancelled. History is kept.")
        else:
            messages.success(self.request, "Trainer details were updated.")
        return response


class TrainerDetailView(GymPermissionMixin, DetailView):
    required_permission = "trainers.view"
    model = Trainer
    template_name = "trainers/trainer_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        t, user = self.object, self.request.user
        today = timezone.localdate()
        assignments = t.assignments.select_related("member")
        if user.role == "trainer":  # trainers see their own roster only
            own = trainer_of(user)
            if own is None or own.pk != t.pk:
                assignments = assignments.none()
        ctx.update(
            current_assignments=[a for a in assignments if a.end_date is None],
            past_assignments=[a for a in assignments if a.end_date is not None],
            availability=t.availability.all(), time_off=t.time_off.filter(end_date__gte=today),
            can_manage=user.has_gym_perm("trainers.manage"),
        )
        if user.has_gym_perm("trainers.manage"):
            ctx["certificates"] = t.certificates.all()
        if user.has_gym_perm("trainers.compensation"):
            ctx["compensation"] = t.compensation.select_related("recorded_by")[:20]
        if user.has_gym_perm("appointments.view"):
            from apps.appointments.models import Appointment, Status

            ctx["completed_sessions"] = Appointment.objects.filter(trainer=t, status=Status.COMPLETED).count()
            ctx["upcoming_sessions"] = Appointment.objects.filter(trainer=t, status=Status.SCHEDULED, start__gt=timezone.now()).count()
        return ctx


class MyMembersView(GymPermissionMixin, ListView):
    """The signed-in trainer's current roster."""

    required_permission = "trainers.view"
    template_name = "trainers/my_members.html"
    paginate_by = 20

    def get_queryset(self):
        qs = scope_members(self.request.user, Member.objects.all()) if self.request.user.role == "trainer" else Member.objects.none()
        return members_with_status(qs).order_by("full_name")


class AssignView(GymPermissionMixin, FormView):
    required_permission = "trainers.manage"
    form_class = forms.AssignForm
    template_name = "trainers/assign_form.html"

    def get_initial(self):
        g = self.request.GET
        return {"trainer": g.get("trainer") if g.get("trainer", "").isdigit() else None, "member": g.get("member", "")}

    def form_valid(self, form):
        c = form.cleaned_data
        try:
            services.assign_trainer(c["trainer"], c["member"], self.request.user, c["notes"])
        except AssignmentError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, f"{c['member'].full_name} was assigned to {c['trainer'].full_name}.")
        return redirect(c["trainer"])


class EndAssignmentView(GymPermissionMixin, View):
    required_permission = "trainers.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        assignment = get_object_or_404(TrainerAssignment, pk=pk)
        try:
            services.end_assignment(assignment, request.user)
            messages.success(request, f"Assignment ended. The history is kept.")
        except AssignmentError as exc:
            messages.error(request, str(exc))
        return redirect(request.POST.get("next") if request.POST.get("next", "").startswith("/") and not request.POST["next"].startswith("//") else assignment.trainer)


# ----------------------------------------------------------------------- availability / time off / certificates
class _ChildCreateView(GymPermissionMixin, FormView):
    required_permission = "trainers.manage"
    template_name = "trainers/child_form.html"
    title = ""
    log_action = ""

    @property
    def trainer(self):
        if not hasattr(self, "_trainer"):
            self._trainer = get_object_or_404(Trainer, pk=self.kwargs["pk"])
        return self._trainer

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=self.title, trainer=self.trainer, multipart=False, **kwargs)

    def save(self, form):
        obj = form.save(commit=False)
        obj.trainer = self.trainer
        obj.save()
        return obj

    def form_valid(self, form):
        self.save(form)
        ActivityLog.record(self.request.user, self.log_action, f"{self.title} – {self.trainer.full_name}")
        messages.success(self.request, "Saved.")
        return redirect(self.trainer)


class AvailabilityCreateView(_ChildCreateView):
    form_class = forms.AvailabilityForm
    title = "Add availability"
    log_action = "trainer_availability"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "trainer": self.trainer}


class TimeOffCreateView(_ChildCreateView):
    form_class = forms.TimeOffForm
    title = "Add time off"
    log_action = "trainer_timeoff"


class CertificateCreateView(_ChildCreateView):
    form_class = forms.CertificateForm
    title = "Upload certificate"
    log_action = "trainer_certificate"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["multipart"] = True
        return ctx

    def save(self, form):
        obj = form.save(commit=False)
        obj.trainer, obj.uploaded_by = self.trainer, self.request.user
        obj.save()
        return obj


class CompensationCreateView(_ChildCreateView):
    required_permission = "trainers.compensation"
    form_class = forms.CompensationForm
    title = "Record salary / commission"
    log_action = "trainer_compensation"

    def save(self, form):
        obj = form.save(commit=False)
        obj.trainer, obj.recorded_by = self.trainer, self.request.user
        obj.save()
        return obj


class _ChildDeleteView(GymPermissionMixin, View):
    required_permission = "trainers.manage"
    http_method_names = ["post"]
    model = None

    def post(self, request, pk):
        obj = get_object_or_404(self.model, pk=pk)
        trainer = obj.trainer
        obj.delete()
        messages.success(request, "Removed.")
        return redirect(trainer)


class AvailabilityDeleteView(_ChildDeleteView):
    model = TrainerAvailability


class TimeOffDeleteView(_ChildDeleteView):
    model = TrainerTimeOff


class CertificateDeleteView(_ChildDeleteView):
    model = TrainerCertificate

    def post(self, request, pk):
        cert = get_object_or_404(TrainerCertificate, pk=pk)
        cert.file.delete(save=False)
        return super().post(request, pk)


class CertificateDownloadView(GymPermissionMixin, View):
    """Certificates live outside the public media folder and are only streamed to authorised staff."""

    required_permission = "trainers.manage"

    def get(self, request, pk):
        cert = get_object_or_404(TrainerCertificate, pk=pk)
        if not cert.file or not cert.file.storage.exists(cert.file.name):
            raise Http404
        ActivityLog.record(request.user, "trainer_certificate_viewed", f"Opened certificate “{cert.title}” of {cert.trainer.full_name}")
        response = FileResponse(cert.file.open("rb"), as_attachment=True, filename=cert.file.name.rsplit("/", 1)[-1])
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

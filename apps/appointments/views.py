from datetime import time, timedelta

from django.contrib import messages
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.accounts.mixins import GymPermissionMixin
from apps.memberships.forms import ReasonForm
from apps.trainers.access import can_access_member, scope_appointments
from apps.trainers.models import Trainer

from . import forms, services
from .models import Appointment, Status
from .services import BookingError


class AppointmentListView(GymPermissionMixin, ListView):
    required_permission = "appointments.view"
    template_name = "appointments/appointment_list.html"
    paginate_by = 20

    def get_queryset(self):
        g, now = self.request.GET, timezone.now()
        qs = scope_appointments(self.request.user, Appointment.objects.select_related("member", "trainer"))
        self.when = g.get("when", "upcoming")
        if self.when == "upcoming":
            qs = qs.filter(status=Status.SCHEDULED, end__gte=now).order_by("start", "pk")
        elif self.when == "past":
            qs = qs.filter(Q(end__lt=now) | ~Q(status=Status.SCHEDULED)).order_by("-start", "-pk")
        else:
            qs = qs.order_by("-start", "-pk")
        if g.get("q", "").strip():
            q = g["q"].strip()
            qs = qs.filter(Q(member__full_name__icontains=q) | Q(member__member_id__icontains=q) | Q(trainer__full_name__icontains=q))
        if g.get("status") in Status.values:
            qs = qs.filter(status=g["status"])
        if g.get("trainer", "").isdigit():
            qs = qs.filter(trainer_id=g["trainer"])
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(start__gte=forms.combine_local(d_from, time.min))
        if d_to:
            qs = qs.filter(start__lt=forms.combine_local(d_to + timedelta(days=1), time.min))
        return qs

    def get_context_data(self, **kwargs):
        g = self.request.GET
        trainers = Trainer.objects.all() if self.request.user.role != "trainer" else Trainer.objects.filter(user=self.request.user)
        return super().get_context_data(
            when=self.when, q=g.get("q", ""), status=g.get("status", ""), trainer=g.get("trainer", ""),
            date_from=g.get("from", ""), date_to=g.get("to", ""), statuses=Status.choices, trainers=trainers,
            filtered_on=any(g.get(k) for k in ("q", "status", "trainer", "from", "to")), **kwargs)


class _ScopedMixin(GymPermissionMixin):
    @cached_property
    def appointment(self):
        return get_object_or_404(scope_appointments(self.request.user, Appointment.objects.select_related("member__user", "trainer__user")), pk=self.kwargs["pk"])


class AppointmentDetailView(_ScopedMixin, DetailView):
    required_permission = "appointments.view"
    template_name = "appointments/appointment_detail.html"

    def get_object(self, queryset=None):
        return self.appointment

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        a, user = self.object, self.request.user
        ctx.update(events=a.events.select_related("user"), can_manage=user.has_gym_perm("appointments.manage"))
        if user.has_gym_perm("payments.view") and a.fee > 0:
            from apps.payments.billing import appointment_billing

            ctx["billing"] = appointment_billing(a)
            ctx["payments"] = a.payments.select_related("recorded_by")
        return ctx


class AppointmentBookView(GymPermissionMixin, FormView):
    required_permission = "appointments.manage"
    form_class = forms.BookingForm
    template_name = "appointments/book.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        trainers = Trainer.objects.filter(employment_status="active")
        if self.request.user.role == "trainer":
            trainers = trainers.filter(user=self.request.user)
        kwargs.update(trainers=trainers, staff=True)
        return kwargs

    def get_initial(self):
        g = self.request.GET
        return {"member": g.get("member", ""), "trainer": g.get("trainer") if g.get("trainer", "").isdigit() else None}

    def form_valid(self, form):
        c = form.cleaned_data
        if not can_access_member(self.request.user, c["member"]):
            form.add_error("member", "You can only book sessions for members assigned to you.")
            return self.form_invalid(form)
        try:
            appt = services.book(c["member"], c["trainer"], form.start(), c["duration"], self.request.user, c["notes"])
        except BookingError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, f"Session booked for {c['member'].full_name}.")
        return redirect(appt)


class _ActionView(_ScopedMixin, FormView):
    required_permission = "appointments.manage"
    template_name = "appointments/action_form.html"
    title, submit_label, danger, intro = "", "Save", False, ""

    def perform(self, form):
        raise NotImplementedError

    def form_valid(self, form):
        try:
            message = self.perform(form)
        except BookingError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, message)
        return redirect(self.appointment)

    def get_context_data(self, **kwargs):
        return super().get_context_data(appointment=self.appointment, title=self.title, submit_label=self.submit_label,
                                        danger=self.danger, intro=self.intro, **kwargs)


class RescheduleView(_ActionView):
    form_class = forms.RescheduleForm
    title, submit_label = "Reschedule session", "Reschedule"
    intro = "The session keeps its length and any payment already made."

    def get_initial(self):
        local = timezone.localtime(self.appointment.start)
        return {"date": local.date(), "time": local.time().replace(second=0, microsecond=0)}

    def perform(self, form):
        services.reschedule(self.appointment, form.start(), self.request.user)
        return "The session was rescheduled."


class CancelView(_ActionView):
    form_class = ReasonForm
    title, submit_label, danger = "Cancel session", "Cancel session", True
    intro = "The member and trainer are notified. If a payment was made, refund it from the payments page."

    def perform(self, form):
        services.cancel(self.appointment, self.request.user, form.cleaned_data["reason"])
        paid = self.appointment.payments.filter(status="completed", kind="payment").exists()
        return "The session was cancelled." + (" A payment exists for it – refund it from Payments if due." if paid else "")


class _OutcomeView(_ScopedMixin, View):
    required_permission = "appointments.manage"
    http_method_names = ["post"]
    outcome = None

    def post(self, request, pk):
        try:
            services.finish(self.appointment, request.user, self.outcome)
            messages.success(request, "The session outcome was recorded.")
        except BookingError as exc:
            messages.error(request, str(exc))
        return redirect(self.appointment)


class CompleteView(_OutcomeView):
    outcome = Status.COMPLETED


class NoShowView(_OutcomeView):
    outcome = Status.NO_SHOW


class SlotsView(GymPermissionMixin, View):
    """JSON list of free start times for a trainer / day / duration (staff booking helper)."""

    required_permission = "appointments.manage"

    def get(self, request):
        g = request.GET
        trainer = get_object_or_404(Trainer, pk=g.get("trainer", "0")) if g.get("trainer", "").isdigit() else None
        day = parse_date(g.get("date", ""))
        duration = int(g["duration"]) if g.get("duration", "").isdigit() else 0
        if trainer is None or day is None:
            return JsonResponse({"slots": []})
        if request.user.role == "trainer" and trainer.user_id != request.user.pk:
            raise Http404
        slots = services.available_slots(trainer, day, duration)
        return JsonResponse({"slots": [timezone.localtime(s).strftime("%H:%M") for s in slots]})

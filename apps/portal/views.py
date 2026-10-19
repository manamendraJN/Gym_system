"""Member portal (/me/...). Every query is filtered on the signed-in member's own record -- ids in URLs are only
ever looked up inside that member's data, so another member's plan, appointment or progress is simply a 404."""
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import FormView, TemplateView

from apps.announcements.services import visible_for
from apps.appointments import forms as appointment_forms
from apps.appointments import services as appointment_services
from apps.appointments.models import Appointment, Status
from apps.appointments.services import BookingError
from apps.memberships.forms import ReasonForm
from apps.memberships.queries import MEMBER_STATUS_LABELS, members_with_status
from apps.members.models import Member
from apps.nutrition import forms as nutrition_forms
from apps.nutrition.models import DISCLAIMER, MealPlan, NutritionProfile
from apps.nutrition.views import group_by_day
from apps.progress import forms as progress_forms
from apps.progress import services as progress_services
from apps.trainers.models import TrainerAssignment, Trainer
from apps.workouts import forms as workout_forms
from apps.workouts import services as workout_services
from apps.workouts.models import PlanStatus, WorkoutPlan
from apps.trainers.models import WEEKDAYS


class PortalMixin:
    """Only signed-in users with the Member role whose login is linked to a member record."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        if request.user.role != "member":
            raise PermissionDenied
        if self.member is None:
            return render(request, "portal/not_linked.html", status=200)
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def member(self):
        try:
            return self.request.user.member_profile
        except Exception:
            return None

    def get_context_data(self, **kwargs):
        return super().get_context_data(member=self.member, **kwargs)

    def my_trainers(self):
        return Trainer.objects.filter(pk__in=TrainerAssignment.objects.filter(member=self.member, end_date__isnull=True).values("trainer"))


class HomeView(PortalMixin, TemplateView):
    template_name = "portal/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.member
        status = members_with_status(Member.objects.filter(pk=m.pk)).get().membership_status
        now = timezone.now()
        ctx.update(
            status_key=status, status_label=MEMBER_STATUS_LABELS[status],
            membership=m.memberships.exclude(status__in=["cancelled", "superseded"]).select_related("plan").order_by("-end_date").first(),
            trainers=self.my_trainers(),
            next_appointments=m.appointments.filter(status=Status.SCHEDULED, end__gte=now).select_related("trainer")[:3],
            plans=m.workout_plans.filter(status=PlanStatus.ACTIVE).select_related("trainer")[:3],
            nutrition_enabled=settings.NUTRITION_ENABLED,
            announcements=visible_for(self.request.user)[:5],
        )
        return ctx


class TrainersView(PortalMixin, TemplateView):
    template_name = "portal/trainers.html"

    def get_context_data(self, **kwargs):
        past = TrainerAssignment.objects.filter(member=self.member, end_date__isnull=False).select_related("trainer")
        return super().get_context_data(trainers=self.my_trainers().prefetch_related("availability"), past=past, **kwargs)


# --------------------------------------------------------------------------------------------- workouts
class WorkoutsView(PortalMixin, TemplateView):
    template_name = "portal/workouts.html"

    def get_context_data(self, **kwargs):
        plans = self.member.workout_plans.select_related("trainer")
        sessions = self.member.workout_sessions.select_related("plan").prefetch_related("entries__exercise")[:10]
        return super().get_context_data(plans=plans, sessions=sessions, **kwargs)


class _MyPlanMixin(PortalMixin):
    @cached_property
    def plan(self):
        return get_object_or_404(WorkoutPlan.objects.select_related("trainer"), pk=self.kwargs["pk"], member=self.member)


class WorkoutDetailView(_MyPlanMixin, TemplateView):
    template_name = "portal/workout_detail.html"

    def get_context_data(self, **kwargs):
        items = list(self.plan.items.filter(is_active=True).select_related("exercise"))
        days = [{"num": n, "label": label, "items": [i for i in items if i.weekday == n]} for n, label in WEEKDAYS]
        return super().get_context_data(plan=self.plan, days=[d for d in days if d["items"]],
                                        sessions=self.plan.sessions.prefetch_related("entries__exercise")[:10], **kwargs)


class LogWorkoutView(_MyPlanMixin, FormView):
    template_name = "workouts/log_form.html"
    form_class = workout_forms.LogWorkoutForm

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        return response

    def get_form_kwargs(self):
        items = self.plan.items.filter(is_active=True).select_related("exercise")
        day = self.request.GET.get("day")
        if day and day.isdigit():
            items = items.filter(weekday=int(day))
        return {**super().get_form_kwargs(), "plan": self.plan, "items": items}

    def get_context_data(self, **kwargs):
        return super().get_context_data(plan=self.plan, portal=True, **kwargs)

    def form_valid(self, form):
        if self.plan.status != PlanStatus.ACTIVE:
            messages.error(self.request, "This plan is no longer active.")
            return redirect("portal:workout", pk=self.plan.pk)
        workout_services.log_session(self.member, self.plan, form.cleaned_data["performed_on"], form.entries(), self.request.user, form.cleaned_data["notes"])
        messages.success(self.request, "Great work! Your workout was recorded.")
        return redirect("portal:workout", pk=self.plan.pk)


# ----------------------------------------------------------------------------------------- appointments
class AppointmentsView(PortalMixin, TemplateView):
    template_name = "portal/appointments.html"

    def get_context_data(self, **kwargs):
        now = timezone.now()
        mine = self.member.appointments.select_related("trainer")
        notice = settings.APPOINTMENT_CHANGE_NOTICE_HOURS
        from datetime import timedelta

        upcoming = list(mine.filter(status=Status.SCHEDULED, end__gte=now).order_by("start"))
        for a in upcoming:
            a.can_change = a.start - now >= timedelta(hours=notice)
        past = mine.exclude(pk__in=[a.pk for a in upcoming]).order_by("-start")[:30]
        return super().get_context_data(upcoming=upcoming, past=past, notice=notice, trainers=self.my_trainers(), **kwargs)


class BookView(PortalMixin, FormView):
    template_name = "portal/book.html"
    form_class = appointment_forms.BookingForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "trainers": self.my_trainers().filter(employment_status="active"), "staff": False}

    def get_initial(self):
        t = self.request.GET.get("trainer")
        return {"trainer": t if t and t.isdigit() else None}

    def form_valid(self, form):
        c = form.cleaned_data
        try:
            appointment_services.book(self.member, c["trainer"], form.start(), c["duration"], self.request.user, c["notes"], as_member=True)
        except BookingError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, "Your session is booked.")
        return redirect("portal:appointments")


class _MyAppointmentMixin(PortalMixin):
    @cached_property
    def appointment(self):
        return get_object_or_404(Appointment.objects.select_related("trainer__user", "member__user"), pk=self.kwargs["pk"], member=self.member)


class RescheduleView(_MyAppointmentMixin, FormView):
    template_name = "portal/appointment_action.html"
    form_class = appointment_forms.RescheduleForm

    def get_initial(self):
        local = timezone.localtime(self.appointment.start)
        return {"date": local.date(), "time": local.time().replace(second=0, microsecond=0)}

    def get_context_data(self, **kwargs):
        return super().get_context_data(appointment=self.appointment, title="Reschedule session", submit_label="Reschedule", danger=False, **kwargs)

    def form_valid(self, form):
        try:
            appointment_services.reschedule(self.appointment, form.start(), self.request.user, as_member=True)
        except BookingError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, "Your session was rescheduled.")
        return redirect("portal:appointments")


class CancelView(_MyAppointmentMixin, FormView):
    template_name = "portal/appointment_action.html"
    form_class = ReasonForm

    def get_context_data(self, **kwargs):
        return super().get_context_data(appointment=self.appointment, title="Cancel session", submit_label="Cancel session", danger=True, **kwargs)

    def form_valid(self, form):
        try:
            appointment_services.cancel(self.appointment, self.request.user, form.cleaned_data["reason"], as_member=True)
        except BookingError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, "Your session was cancelled.")
        return redirect("portal:appointments")


class SlotsView(PortalMixin, View):
    http_method_names = ["get"]

    def get(self, request):
        g = request.GET
        day, duration = parse_date(g.get("date", "")), int(g["duration"]) if g.get("duration", "").isdigit() else 0
        trainer = self.my_trainers().filter(pk=g["trainer"]).first() if g.get("trainer", "").isdigit() else None
        if trainer is None or day is None:
            return JsonResponse({"slots": []})
        slots = appointment_services.available_slots(trainer, day, duration, as_member=True)
        return JsonResponse({"slots": [timezone.localtime(s).strftime("%H:%M") for s in slots]})


# ------------------------------------------------------------------------------------------- progress
class ProgressView(PortalMixin, FormView):
    template_name = "progress/member_progress.html"
    form_class = progress_forms.ProgressForm

    def get_context_data(self, **kwargs):
        records = list(self.member.progress_records.prefetch_related("measurements__type"))
        return super().get_context_data(records=records, chart=progress_services.chart_data(self.member), can_add=True, portal=True, **kwargs)

    def form_valid(self, form):
        progress_services.add_record(self.member, form, self.request.user, "member")
        messages.success(self.request, "Your progress was saved.")
        return redirect("portal:progress")


# ------------------------------------------------------------------------------------------ nutrition
class _NutritionMixin(PortalMixin):
    def dispatch(self, request, *args, **kwargs):
        if not settings.NUTRITION_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class NutritionView(_NutritionMixin, FormView):
    """Own meal plans (read-only) and own food preferences / allergies / goals (editable)."""

    template_name = "portal/nutrition.html"
    form_class = nutrition_forms.ProfileForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": NutritionProfile.objects.filter(member=self.member).first()}

    def get_context_data(self, **kwargs):
        plans = self.member.meal_plans.filter(is_active=True)
        return super().get_context_data(plans=[{"plan": p, "days": group_by_day(list(p.items.all()))} for p in plans], disclaimer=DISCLAIMER, **kwargs)

    def form_valid(self, form):
        profile = form.save(commit=False)
        profile.member, profile.updated_by = self.member, self.request.user
        profile.save()
        messages.success(self.request, "Your nutrition profile was saved.")
        return redirect("portal:nutrition")


# ------------------------------------------------------------------------------------------- payments
class PaymentsView(PortalMixin, TemplateView):
    """The member's own fees, balances and payment history (never another member's)."""

    template_name = "portal/payments.html"

    def get_context_data(self, **kwargs):
        from apps.payments.billing import with_billing
        from apps.payments.models import Kind, Status

        memberships = with_billing(self.member.memberships.exclude(status="cancelled").select_related("plan"), "price").order_by("-start_date")
        sessions = with_billing(self.member.appointments.exclude(status="cancelled").filter(fee__gt=0).select_related("trainer"), "fee").order_by("-start")
        payments = self.member.payments.exclude(kind=Kind.ADJUSTMENT).exclude(status__in=[Status.FAILED, Status.VOIDED]).select_related(
            "membership__plan", "appointment__trainer")
        owed = sum((m.outstanding for m in memberships), 0) + sum((a.outstanding for a in sessions), 0)
        return super().get_context_data(memberships=memberships, sessions=sessions, payments=payments[:50], owed=owed, **kwargs)


class _MyPaymentMixin(PortalMixin):
    @cached_property
    def payment(self):
        from apps.payments.models import Kind, Payment

        qs = Payment.objects.filter(member=self.member).exclude(kind=Kind.ADJUSTMENT).select_related(
            "member", "membership__plan", "appointment__trainer", "recorded_by", "refund_of")
        return get_object_or_404(qs, pk=self.kwargs["pk"])


class ReceiptView(_MyPaymentMixin, TemplateView):
    template_name = "payments/receipt.html"

    def get_context_data(self, **kwargs):
        from apps.payments import receipts

        # receipt_data already carries `member` (this member), so bypass PortalMixin's own `member` entry
        return TemplateView.get_context_data(self, **receipts.receipt_data(self.payment), portal=True, **kwargs)


class ReceiptPDFView(_MyPaymentMixin, View):
    http_method_names = ["get"]

    def get(self, request, pk):
        from django.http import HttpResponse

        from apps.payments import receipts

        response = HttpResponse(receipts.render_pdf(self.payment), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{self.payment.receipt_number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone


class Status(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    NO_SHOW = "no_show", "No-show"


class Appointment(models.Model):
    """One personal-training session. Rescheduling edits start/end in place (the payment stays attached) and
    every change is recorded in AppointmentEvent."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="appointments")
    trainer = models.ForeignKey("trainers.Trainer", on_delete=models.PROTECT, related_name="appointments")
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    # Snapshot of trainer rate x duration at booking time (0 = free). Payments for it use the normal payment ledger.
    fee = models.DecimalField("fee (LKR)", max_digits=10, decimal_places=2, default=0)
    notes = models.CharField(max_length=255, blank=True)
    booked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    booked_via = models.CharField(max_length=10, default="staff")  # staff | portal
    cancel_reason = models.CharField(max_length=255, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start", "id"]
        indexes = [models.Index(fields=["trainer", "status", "start"]), models.Index(fields=["member", "status", "start"])]
        constraints = [
            models.CheckConstraint(condition=Q(end__gt=F("start")), name="appointment_end_after_start"),
            models.CheckConstraint(condition=Q(fee__gte=0), name="appointment_fee_non_negative"),
            # Backstop against double-booking the exact same slot even if application locking were bypassed.
            models.UniqueConstraint(fields=["trainer", "start"], condition=Q(status="scheduled"), name="one_scheduled_session_per_trainer_start"),
        ]

    def __str__(self):
        return f"{self.member} with {self.trainer} on {self.start:%Y-%m-%d %H:%M}"

    def get_absolute_url(self):
        return reverse("appointments:detail", args=[self.pk])

    @property
    def duration_minutes(self):
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_upcoming(self):
        return self.status == Status.SCHEDULED and self.start > timezone.now()

    @property
    def has_started(self):
        return self.start <= timezone.now()

    @property
    def is_scheduled(self):
        return self.status == Status.SCHEDULED

    @property
    def status_key(self):
        """Badge key: scheduled sessions whose time has passed are shown as 'awaiting outcome'."""
        if self.status == Status.SCHEDULED and self.end < timezone.now():
            return "awaiting"
        return self.status

    @property
    def status_label(self):
        return "Awaiting outcome" if self.status_key == "awaiting" else self.get_status_display()


class AppointmentEvent(models.Model):
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=20)
    detail = models.CharField(max_length=255, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

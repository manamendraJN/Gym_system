import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import phone_validator
from apps.core.storage import PrivateStorage

WEEKDAYS = [(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"), (6, "Sunday")]


def trainer_photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"trainers/photos/{uuid.uuid4().hex}.{ext}"


def certificate_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"trainers/certificates/{instance.trainer_id}/{uuid.uuid4().hex}.{ext}"


class EmploymentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ON_LEAVE = "on_leave", "On leave"
    TERMINATED = "terminated", "Left the gym"


class Trainer(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="trainer_profile", help_text="The trainer's login (role: Trainer).")
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, validators=[phone_validator])
    photo = models.ImageField(upload_to=trainer_photo_path, blank=True)
    bio = models.TextField(blank=True)
    qualifications = models.TextField(blank=True)
    specialties = models.CharField(max_length=255, blank=True, help_text="Comma-separated, e.g. Strength, Yoga, Rehab.")
    employment_status = models.CharField(max_length=12, choices=EmploymentStatus.choices, default=EmploymentStatus.ACTIVE, db_index=True)
    hire_date = models.DateField(default=timezone.localdate)
    hourly_rate = models.DecimalField("personal training rate (LKR / hour)", max_digits=10, decimal_places=2, default=0,
                                      help_text="0 = sessions are free. The fee is copied onto each appointment when booked.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name"]
        constraints = [models.CheckConstraint(condition=Q(hourly_rate__gte=0), name="trainer_rate_non_negative")]

    def __str__(self):
        return self.full_name

    def get_absolute_url(self):
        return reverse("trainers:detail", args=[self.pk])

    @property
    def is_bookable(self):
        return self.employment_status == EmploymentStatus.ACTIVE

    @property
    def specialty_list(self):
        return [s.strip() for s in self.specialties.split(",") if s.strip()]

    @property
    def initials(self):
        return (self.full_name.strip()[:1] or "?").upper()


class TrainerCertificate(models.Model):
    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="certificates")
    title = models.CharField(max_length=150)
    issuer = models.CharField(max_length=150, blank=True)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    file = models.FileField(upload_to=certificate_path, storage=PrivateStorage())
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.trainer}: {self.title}"

    @property
    def is_expired(self):
        return bool(self.expires_on and self.expires_on < timezone.localdate())


class TrainerAssignment(models.Model):
    """Which trainer looks after which member. Ending an assignment keeps the row (training history)."""

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="assignments")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="trainer_assignments")
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["trainer", "member"], condition=Q(end_date__isnull=True), name="one_open_assignment_per_pair"),
            models.CheckConstraint(condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")), name="assignment_end_after_start"),
        ]

    def __str__(self):
        return f"{self.trainer} → {self.member}"

    @property
    def is_active(self):
        return self.end_date is None


class TrainerAvailability(models.Model):
    """A recurring weekly window in which the trainer can be booked (local time)."""

    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="availability")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(condition=Q(end_time__gt=F("start_time")), name="availability_end_after_start"),
            models.CheckConstraint(condition=Q(weekday__gte=0, weekday__lte=6), name="availability_weekday_range"),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.start_time:%H:%M}–{self.end_time:%H:%M}"


class TrainerTimeOff(models.Model):
    trainer = models.ForeignKey(Trainer, on_delete=models.CASCADE, related_name="time_off")
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="timeoff_end_after_start")]


class CompensationKind(models.TextChoices):
    SALARY = "salary", "Salary"
    COMMISSION = "commission", "Commission"
    BONUS = "bonus", "Bonus"


class TrainerCompensation(models.Model):
    """Salary / commission record. Append-only, visible only with trainers.compensation."""

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="compensation")
    kind = models.CharField(max_length=12, choices=CompensationKind.choices)
    amount = models.DecimalField("amount (LKR)", max_digits=12, decimal_places=2)
    period_start = models.DateField()
    period_end = models.DateField()
    notes = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_end", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="compensation_positive"),
            models.CheckConstraint(condition=Q(period_end__gte=F("period_start")), name="compensation_period_valid"),
        ]

    def delete(self, *args, **kwargs):
        raise ValueError("Compensation records cannot be deleted.")

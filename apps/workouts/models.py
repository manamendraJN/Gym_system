import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.trainers.models import WEEKDAYS


def exercise_image_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"exercises/{uuid.uuid4().hex}.{ext}"


class MuscleGroup(models.TextChoices):
    CHEST = "chest", "Chest"
    BACK = "back", "Back"
    SHOULDERS = "shoulders", "Shoulders"
    ARMS = "arms", "Arms"
    CORE = "core", "Core"
    LEGS = "legs", "Legs"
    GLUTES = "glutes", "Glutes"
    FULL_BODY = "full_body", "Full body"
    CARDIO = "cardio", "Cardio"


class Category(models.TextChoices):
    STRENGTH = "strength", "Strength"
    CARDIO = "cardio", "Cardio"
    FLEXIBILITY = "flexibility", "Flexibility / mobility"
    BALANCE = "balance", "Balance / stability"
    FUNCTIONAL = "functional", "Functional / HIIT"
    OTHER = "other", "Other"


class Exercise(models.Model):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    muscle_group = models.CharField(max_length=12, choices=MuscleGroup.choices, db_index=True)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.STRENGTH, db_index=True)
    instructions = models.TextField(blank=True)
    image = models.ImageField(upload_to=exercise_image_path, blank=True)
    video_url = models.URLField(blank=True, help_text="Link to a demonstration video (https only).")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="unique_exercise_name_ci")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("workouts:exercise_detail", args=[self.pk])


class PlanStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    ARCHIVED = "archived", "Archived"


class WorkoutPlan(models.Model):
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="workout_plans")
    trainer = models.ForeignKey("trainers.Trainer", null=True, blank=True, on_delete=models.SET_NULL, related_name="workout_plans")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    goal = models.CharField(max_length=200, blank=True)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=PlanStatus.choices, default=PlanStatus.ACTIVE, db_index=True)
    copied_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="copies")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")), name="workoutplan_end_after_start")]

    def __str__(self):
        return f"{self.name} ({self.member})"

    def get_absolute_url(self):
        return reverse("workouts:plan_detail", args=[self.pk])

    @property
    def is_current(self):
        today = timezone.localdate()
        return self.status == PlanStatus.ACTIVE and self.start_date <= today and (self.end_date is None or self.end_date >= today)

    @property
    def status_key(self):
        if self.status == PlanStatus.ACTIVE:
            today = timezone.localdate()
            if self.start_date > today:
                return "scheduled"
            if self.end_date and self.end_date < today:
                return "expired"
        return self.status

    @property
    def status_label(self):
        return {"scheduled": "Starts later", "expired": "Past end date"}.get(self.status_key, self.get_status_display())


class PlanExercise(models.Model):
    """An exercise prescribed on a given day. Rows that already have logged workouts are archived, never deleted,
    so past sessions keep their meaning."""

    plan = models.ForeignKey(WorkoutPlan, on_delete=models.CASCADE, related_name="items")
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT, related_name="plan_items")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    position = models.PositiveSmallIntegerField(default=0)
    sets = models.PositiveSmallIntegerField(default=3)
    reps = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    rest_seconds = models.PositiveSmallIntegerField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "position", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(sets__gte=1), name="planexercise_sets_positive"),
            models.CheckConstraint(condition=Q(reps__isnull=False) | Q(duration_minutes__isnull=False), name="planexercise_reps_or_duration"),
            models.CheckConstraint(condition=Q(weekday__gte=0, weekday__lte=6), name="planexercise_weekday_range"),
        ]

    def __str__(self):
        return f"{self.exercise} ({self.get_weekday_display()})"

    @property
    def prescription(self):
        parts = [f"{self.sets} × {self.reps}" if self.reps else f"{self.sets} sets"]
        if self.duration_minutes:
            parts.append(f"{self.duration_minutes} min")
        if self.weight_kg:
            parts.append(f"{self.weight_kg.normalize():f} kg")
        if self.rest_seconds:
            parts.append(f"rest {self.rest_seconds}s")
        return " · ".join(parts)


class PlanChange(models.Model):
    """Change log of a plan (who changed what, when)."""

    plan = models.ForeignKey(WorkoutPlan, on_delete=models.CASCADE, related_name="changes")
    description = models.CharField(max_length=255)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WorkoutSession(models.Model):
    """A workout that was actually performed. Entries store what was done (a snapshot), independent of later plan edits."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="workout_sessions")
    plan = models.ForeignKey(WorkoutPlan, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions")
    performed_on = models.DateField(default=timezone.localdate, db_index=True)
    notes = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-performed_on", "-id"]


class SessionEntry(models.Model):
    session = models.ForeignKey(WorkoutSession, on_delete=models.CASCADE, related_name="entries")
    plan_item = models.ForeignKey(PlanExercise, null=True, blank=True, on_delete=models.SET_NULL, related_name="log_entries")
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT, related_name="log_entries")
    sets_done = models.PositiveSmallIntegerField(default=0)
    reps_done = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

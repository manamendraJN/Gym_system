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


# --------------------------------------------------------------------------------- live workout tracking
class RunStatus(models.TextChoices):
    ACTIVE = "active", "In progress"
    COMPLETED = "completed", "Completed"
    ABANDONED = "abandoned", "Ended early"


class WorkoutRun(models.Model):
    """A guided workout the member runs from the portal (start → exercises → sets with rest timers).
    All timestamps are written by the server; the browser only displays countdowns. When a run ends, a normal
    WorkoutSession (history entry) is created from what was actually done."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="workout_runs")
    plan = models.ForeignKey(WorkoutPlan, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    status = models.CharField(max_length=10, choices=RunStatus.choices, default=RunStatus.ACTIVE, db_index=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    session = models.OneToOneField(WorkoutSession, null=True, blank=True, on_delete=models.SET_NULL, related_name="run")

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["member"], condition=Q(status="active"), name="one_active_run_per_member"),
            models.CheckConstraint(condition=Q(completed_at__isnull=True) | Q(completed_at__gte=F("started_at")), name="run_end_after_start"),
        ]

    def __str__(self):
        return f"{self.member} – {self.plan} ({self.started_at:%Y-%m-%d %H:%M})"

    @property
    def duration(self):
        return (self.completed_at or timezone.now()) - self.started_at

    def get_absolute_url(self):
        return reverse("portal:run", args=[self.pk])


class ExerciseStatus(models.TextChoices):
    PENDING = "pending", "Upcoming"
    ACTIVE = "active", "In progress"
    COMPLETED = "completed", "Completed"
    SKIPPED = "skipped", "Not done"


class ExerciseRun(models.Model):
    """One exercise of a run. Targets are copied from the plan when the run starts so later plan edits don't change it."""

    run = models.ForeignKey(WorkoutRun, on_delete=models.CASCADE, related_name="exercises")
    plan_item = models.ForeignKey(PlanExercise, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT, related_name="runs")
    position = models.PositiveSmallIntegerField()
    target_sets = models.PositiveSmallIntegerField()
    target_reps = models.PositiveSmallIntegerField(null=True, blank=True)
    target_duration_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    target_weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rest_seconds = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=10, choices=ExerciseStatus.choices, default=ExerciseStatus.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "position"], name="unique_exercise_position_in_run"),
            models.UniqueConstraint(fields=["run"], condition=Q(status="active"), name="one_active_exercise_per_run"),
        ]

    @property
    def duration(self):
        if not self.started_at:
            return None
        return (self.completed_at or timezone.now()) - self.started_at

    @property
    def sets_done(self):
        return sum(1 for s in self.sets.all() if s.completed_at)


class SetRun(models.Model):
    """One set. `rest_*` describe the rest that followed the previous set: configured length and when it actually ended
    (= when this set was started)."""

    exercise_run = models.ForeignKey(ExerciseRun, on_delete=models.CASCADE, related_name="sets")
    number = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    rest_before_seconds = models.PositiveSmallIntegerField(default=0, help_text="Configured rest that preceded this set.")
    rest_before_actual = models.DurationField(null=True, blank=True, help_text="How long the member actually rested before this set.")

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["exercise_run", "number"], name="unique_set_number"),
            models.CheckConstraint(condition=Q(completed_at__isnull=True) | Q(completed_at__gte=F("started_at")), name="set_end_after_start"),
        ]

    @property
    def elapsed(self):
        return (self.completed_at or timezone.now()) - self.started_at

from django.db import transaction
from django.utils import timezone

from apps.core.models import ActivityLog, Notification

from .models import PlanChange, PlanExercise, PlanStatus, SessionEntry, WorkoutPlan, WorkoutSession


def note_change(plan, user, text):
    PlanChange.objects.create(plan=plan, user=user, description=text[:255])


@transaction.atomic
def create_plan(member, form, user, trainer=None):
    plan = form.save(commit=False)
    plan.member, plan.trainer, plan.created_by = member, trainer, user
    plan.save()
    note_change(plan, user, "Plan created")
    ActivityLog.record(user, "workout_plan_created", f"Created workout plan “{plan.name}” for {member.full_name}")
    if member.user_id:
        Notification.send(member.user, "New workout plan", f"“{plan.name}” was assigned to you.", f"/me/workouts/{plan.pk}/")
    return plan


@transaction.atomic
def copy_plan(plan, member, start_date, user, trainer=None):
    """Assign a copy of a plan (active exercises only) to another member. The original is untouched."""
    new = WorkoutPlan.objects.create(
        member=member, trainer=trainer or plan.trainer, name=plan.name, description=plan.description, goal=plan.goal,
        start_date=start_date, end_date=None, copied_from=plan, created_by=user)
    PlanExercise.objects.bulk_create([
        PlanExercise(plan=new, exercise=i.exercise, weekday=i.weekday, position=i.position, sets=i.sets, reps=i.reps,
                     weight_kg=i.weight_kg, duration_minutes=i.duration_minutes, rest_seconds=i.rest_seconds, notes=i.notes)
        for i in plan.items.filter(is_active=True)])
    note_change(new, user, f"Copied from plan #{plan.pk} ({plan.member.full_name})")
    ActivityLog.record(user, "workout_plan_created", f"Copied workout plan “{plan.name}” to {member.full_name}")
    if member.user_id:
        Notification.send(member.user, "New workout plan", f"“{new.name}” was assigned to you.", f"/me/workouts/{new.pk}/")
    return new


@transaction.atomic
def remove_item(item, user):
    """Delete an exercise from a plan, or archive it when workouts were already logged against it (history kept)."""
    plan, name = item.plan, item.exercise.name
    if item.log_entries.exists():
        item.is_active = False
        item.save(update_fields=["is_active"])
        note_change(plan, user, f"Archived “{name}” (it has logged workouts)")
        return "archived"
    item.delete()
    note_change(plan, user, f"Removed “{name}”")
    return "deleted"


@transaction.atomic
def set_status(plan, status, user):
    plan.status = status
    plan.save(update_fields=["status", "updated_at"])
    note_change(plan, user, f"Status set to {PlanStatus(status).label}")


@transaction.atomic
def log_session(member, plan, performed_on, entries, user, notes=""):
    """Record a performed workout. Entries copy the numbers actually done, so later plan edits never rewrite history."""
    session = WorkoutSession.objects.create(member=member, plan=plan, performed_on=performed_on, notes=notes[:255], recorded_by=user)
    SessionEntry.objects.bulk_create([
        SessionEntry(session=session, plan_item=e["item"], exercise=e["item"].exercise, sets_done=e["sets"], reps_done=e["reps"],
                     weight_kg=e["weight"], duration_minutes=e["duration"])
        for e in entries])
    ActivityLog.record(user, "workout_logged", f"Workout recorded for {member.full_name} ({len(entries)} exercises)")
    return session

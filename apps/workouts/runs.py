"""Guided workout runs: start a workout, run each exercise set by set with rest timers, and keep a server-side record.

Rules (all enforced here, never in the browser):
* one active run per member; the plan must be active and the chosen weekday must have exercises;
* exercises run strictly in the trainer's order and only one is active at a time; the next exercise starts only on request;
* finishing a set starts the rest period (configured rest of the exercise); the next set can only be started after the rest
  has elapsed (server clock, small grace), and never starts by itself;
* finishing the last set completes the exercise; finishing the last exercise completes the run;
* ending early keeps what was done; stale runs are closed automatically;
* on completion a normal WorkoutSession is created so the existing history and reports keep working."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import ActivityLog

from .models import (ExerciseRun, ExerciseStatus, PlanStatus, RunStatus, SessionEntry, SetRun, WorkoutRun, WorkoutSession)

RUN_MAX_HOURS = 12          # an active run older than this is closed automatically
REST_GRACE_SECONDS = 1.5    # tolerance for clock rounding between the displayed countdown and the server check


class RunError(Exception):
    """A rule was broken; the message is safe to show to the member."""


def _locked_run(run):
    return WorkoutRun.objects.select_for_update().select_related("plan", "member").get(pk=run.pk)


def _require_active(run):
    if run.status != RunStatus.ACTIVE:
        raise RunError("This workout has already ended.")


def rest_ends_at(exercise_run, previous_set):
    return previous_set.completed_at + timedelta(seconds=exercise_run.rest_seconds)


# ------------------------------------------------------------------------------------------------ start
@transaction.atomic
def start_run(member, plan, weekday, now=None):
    now = now or timezone.now()
    close_stale_runs(member, now)
    if plan.member_id != member.pk:
        raise RunError("This plan is not assigned to you.")
    if plan.status != PlanStatus.ACTIVE:
        raise RunError("This plan is no longer active.")
    existing = WorkoutRun.objects.filter(member=member, status=RunStatus.ACTIVE).first()
    if existing:
        return existing, False  # refreshing or double-clicking never creates a second run
    items = list(plan.items.filter(is_active=True, weekday=weekday).select_related("exercise").order_by("position", "id"))
    if not items:
        raise RunError("There are no exercises planned for that day.")
    try:
        with transaction.atomic():
            run = WorkoutRun.objects.create(member=member, plan=plan, weekday=weekday, started_at=now)
    except IntegrityError:  # lost a race with another tab
        return WorkoutRun.objects.get(member=member, status=RunStatus.ACTIVE), False
    ExerciseRun.objects.bulk_create([
        ExerciseRun(run=run, plan_item=i, exercise=i.exercise, position=n, target_sets=i.sets, target_reps=i.reps,
                    target_duration_minutes=i.duration_minutes, target_weight_kg=i.weight_kg, rest_seconds=i.rest_seconds or 0)
        for n, i in enumerate(items, start=1)])
    return run, True


# ------------------------------------------------------------------------------------------- exercises
@transaction.atomic
def start_exercise(run, exercise_pk, now=None):
    now = now or timezone.now()
    run = _locked_run(run)
    _require_active(run)
    exercises = list(run.exercises.select_for_update().order_by("position"))
    target = next((e for e in exercises if e.pk == exercise_pk), None)
    if target is None:
        raise RunError("That exercise is not part of this workout.")
    if target.status == ExerciseStatus.ACTIVE:
        return target  # double click: already started
    if target.status != ExerciseStatus.PENDING:
        raise RunError("That exercise is already completed.")
    if any(e.status == ExerciseStatus.ACTIVE for e in exercises):
        raise RunError("Finish the current exercise first.")
    if any(e.position < target.position and e.status != ExerciseStatus.COMPLETED for e in exercises):
        raise RunError("Complete the exercises in order.")
    target.status, target.started_at = ExerciseStatus.ACTIVE, now
    target.save(update_fields=["status", "started_at"])
    SetRun.objects.create(exercise_run=target, number=1, started_at=now)
    return target


@transaction.atomic
def finish_set(run, set_pk, now=None):
    """Complete the open set. Returns (set, exercise_completed, run_completed)."""
    now = now or timezone.now()
    run = _locked_run(run)
    _require_active(run)
    s = SetRun.objects.select_for_update().select_related("exercise_run").filter(pk=set_pk, exercise_run__run=run).first()
    if s is None:
        raise RunError("That set is not part of this workout.")
    if s.completed_at:
        raise RunError("This set was already finished.")
    ex = s.exercise_run
    if ex.status != ExerciseStatus.ACTIVE:
        raise RunError("This exercise is not in progress.")
    s.completed_at = max(now, s.started_at)
    s.save(update_fields=["completed_at"])
    exercise_done = s.number >= ex.target_sets
    run_done = False
    if exercise_done:
        ex.status, ex.completed_at = ExerciseStatus.COMPLETED, s.completed_at
        ex.save(update_fields=["status", "completed_at"])
        if not run.exercises.exclude(status=ExerciseStatus.COMPLETED).exists():
            finalize(run, RunStatus.COMPLETED, now)
            run_done = True
    return s, exercise_done, run_done


@transaction.atomic
def start_next_set(run, exercise_pk, now=None):
    now = now or timezone.now()
    run = _locked_run(run)
    _require_active(run)
    ex = run.exercises.select_for_update().filter(pk=exercise_pk).first()
    if ex is None or ex.status != ExerciseStatus.ACTIVE:
        raise RunError("This exercise is not in progress.")
    sets = list(ex.sets.select_for_update().order_by("number"))
    last = sets[-1]
    if last.completed_at is None:
        return last  # double click: the set is already running
    if len(sets) >= ex.target_sets:
        raise RunError("All sets of this exercise are done.")
    ends = rest_ends_at(ex, last)
    if (ends - now).total_seconds() > REST_GRACE_SECONDS:
        raise RunError("Rest is not finished yet.")
    return SetRun.objects.create(exercise_run=ex, number=last.number + 1, started_at=now, rest_before_seconds=ex.rest_seconds,
                                 rest_before_actual=now - last.completed_at)


# ----------------------------------------------------------------------------------------------- ending
@transaction.atomic
def end_run(run, now=None):
    run = _locked_run(run)
    _require_active(run)
    finalize(run, RunStatus.ABANDONED, now or timezone.now())
    return run


def close_stale_runs(member, now=None):
    now = now or timezone.now()
    for run in WorkoutRun.objects.filter(member=member, status=RunStatus.ACTIVE, started_at__lt=now - timedelta(hours=RUN_MAX_HOURS)):
        with transaction.atomic():
            run = _locked_run(run)
            if run.status == RunStatus.ACTIVE:
                finalize(run, RunStatus.ABANDONED, run.started_at + timedelta(hours=RUN_MAX_HOURS))


def finalize(run, status, now):
    """Close the run, mark untouched exercises as not done, close an unfinished set, and write the history entry."""
    for ex in run.exercises.select_for_update().prefetch_related("sets"):
        if ex.status == ExerciseStatus.PENDING:
            ex.status = ExerciseStatus.SKIPPED
            ex.save(update_fields=["status"])
        elif ex.status == ExerciseStatus.ACTIVE:
            ex.completed_at = now  # an unfinished set keeps no completion time and is not counted as done
            ex.status = ExerciseStatus.COMPLETED if ex.sets_done >= ex.target_sets else ExerciseStatus.SKIPPED
            ex.save(update_fields=["status", "completed_at"])
    run.status, run.completed_at = status, max(now, run.started_at)
    session = WorkoutSession.objects.create(
        member=run.member, plan=run.plan, performed_on=timezone.localtime(run.started_at).date(), recorded_by=run.member.user,
        notes=f"Guided workout ({'completed' if status == RunStatus.COMPLETED else 'ended early'})")
    entries = [SessionEntry(session=session, plan_item=ex.plan_item, exercise=ex.exercise, sets_done=ex.sets_done, reps_done=ex.target_reps,
                            weight_kg=ex.target_weight_kg, duration_minutes=ex.target_duration_minutes)
               for ex in run.exercises.all().prefetch_related("sets") if ex.sets_done]
    SessionEntry.objects.bulk_create(entries)
    run.session = session
    run.save(update_fields=["status", "completed_at", "session"])
    ActivityLog.record(run.member.user, "workout_run_" + status, f"{run.member.full_name} {'completed' if status == RunStatus.COMPLETED else 'ended'} a guided workout ({len(entries)} exercises)")


# ------------------------------------------------------------------------------------------ presenting
def build_state(run, now=None):
    """Everything the page needs, computed from stored timestamps only."""
    now = now or timezone.now()
    exercises = list(run.exercises.select_related("exercise").prefetch_related("sets").order_by("position"))
    total_sets = sum(e.target_sets for e in exercises)
    done_sets = sum(e.sets_done for e in exercises)
    state = {"exercises": exercises, "total_sets": total_sets, "done_sets": done_sets,
             "percent": round(100 * done_sets / total_sets) if total_sets else 0,
             "done_exercises": sum(1 for e in exercises if e.status == ExerciseStatus.COMPLETED),
             "current": None, "phase": None}
    for e in exercises:
        sets = list(e.sets.all())
        e.set_list = sets
        e.done_count = sum(1 for s in sets if s.completed_at)
        e.phase = e.status
        if e.status == ExerciseStatus.ACTIVE:
            last = sets[-1]
            e.open_set = last if last.completed_at is None else None
            if e.open_set:
                e.phase, e.set_number = "working", last.number
            else:
                ends = rest_ends_at(e, last)
                e.rest_ends = ends
                e.rest_remaining = max(0, (ends - now).total_seconds())
                e.phase = "resting" if e.rest_remaining > 0 else "ready"
                e.set_number = last.number + 1
            state["current"] = e
            state["phase"] = e.phase
    if run.status == RunStatus.ACTIVE and state["current"] is None:
        state["next_exercise"] = next((e for e in exercises if e.status == ExerciseStatus.PENDING), None)
    else:
        state["next_exercise"] = None
    return state

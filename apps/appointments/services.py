"""Appointment rules.

A booking is accepted only if, checked on the server inside a transaction that locks the trainer and the member:
  * the duration is one of APPOINTMENT_DURATIONS, the start is on the slot grid and within the booking horizon;
  * the trainer is employed (active), the member is active, and (optionally) holds a membership covering that day;
  * the whole session fits inside one of the trainer's weekly availability windows (local time) and not in time off;
  * the trainer has no overlapping scheduled session, and neither does the member;
  * members booking for themselves: only with a currently assigned trainer, with minimum notice.
Reschedules and cancellations by members need APPOINTMENT_CHANGE_NOTICE_HOURS notice; staff may override.
A partial unique index (trainer, start) for scheduled sessions is a last line of defence against double booking.
Fees use the existing payment ledger (see payments.services); this module never stores payments itself.
"""
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import ActivityLog, Notification
from apps.members.models import Member
from apps.memberships.models import Membership, MembershipStatus
from apps.trainers.models import Trainer, TrainerAssignment, TrainerAvailability, TrainerTimeOff

from .models import Appointment, AppointmentEvent, Status


class BookingError(Exception):
    """A rule prevented the operation; the message is safe to show to the user."""


def fee_for(trainer, duration):
    return (Decimal(trainer.hourly_rate) * duration / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def has_active_assignment(trainer, member, today=None):
    return TrainerAssignment.objects.filter(trainer=trainer, member=member, end_date__isnull=True).exists()


def _overlapping(qs, start, end, exclude=None):
    qs = qs.filter(status=Status.SCHEDULED, start__lt=end, end__gt=start)
    return qs.exclude(pk=exclude.pk) if exclude else qs


def validate_slot(trainer, member, start, duration, *, as_member=False, exclude=None, now=None):
    """Raise BookingError unless this exact session may be scheduled. Returns the end datetime."""
    now = now or timezone.now()
    if duration not in settings.APPOINTMENT_DURATIONS:
        raise BookingError("Choose one of the available session lengths.")
    step = settings.APPOINTMENT_SLOT_STEP_MINUTES
    local_start = timezone.localtime(start)
    if local_start.minute % step or local_start.second or local_start.microsecond:
        raise BookingError(f"Sessions must start on a {step}-minute mark (for example 09:00, 09:{step:02d}).")
    end = start + timedelta(minutes=duration)
    local_end = timezone.localtime(end)
    if start <= now:
        raise BookingError("The session must start in the future.")
    if as_member and start < now + timedelta(minutes=settings.APPOINTMENT_MIN_LEAD_MINUTES):
        raise BookingError(f"Please book at least {settings.APPOINTMENT_MIN_LEAD_MINUTES} minutes ahead.")
    if start > now + timedelta(days=settings.APPOINTMENT_MAX_ADVANCE_DAYS):
        raise BookingError(f"Sessions can be booked at most {settings.APPOINTMENT_MAX_ADVANCE_DAYS} days ahead.")
    if not trainer.is_bookable:
        raise BookingError(f"{trainer.full_name} is not taking bookings at the moment.")
    if not member.is_active:
        raise BookingError("This member is deactivated.")
    if local_end.date() != local_start.date():
        raise BookingError("A session cannot run past midnight.")
    fits = TrainerAvailability.objects.filter(
        trainer=trainer, weekday=local_start.weekday(), start_time__lte=local_start.time(), end_time__gte=local_end.time()).exists()
    if not fits:
        raise BookingError("The trainer is not available at that time.")
    if TrainerTimeOff.objects.filter(trainer=trainer, start_date__lte=local_start.date(), end_date__gte=local_start.date()).exists():
        raise BookingError("The trainer is on leave on that day.")
    if _overlapping(Appointment.objects.filter(trainer=trainer), start, end, exclude).exists():
        raise BookingError("The trainer already has a session that overlaps this time.")
    if _overlapping(Appointment.objects.filter(member=member), start, end, exclude).exists():
        raise BookingError("The member already has a session that overlaps this time.")
    if settings.APPOINTMENT_REQUIRE_MEMBERSHIP and not Membership.objects.filter(
            member=member, status=MembershipStatus.ACTIVE, start_date__lte=local_start.date(), end_date__gte=local_start.date()).exists():
        raise BookingError("An active membership covering that day is required to book a session.")
    if as_member and not has_active_assignment(trainer, member, local_start.date()):
        raise BookingError("You can only book sessions with your assigned trainer.")
    return end


def _event(appt, action, user, detail=""):
    AppointmentEvent.objects.create(appointment=appt, action=action, user=user, detail=detail[:255])


def _notify(appt, actor, title, message):
    """Tell the other parties (member and trainer logins), not the person who acted."""
    for user in (appt.member.user, appt.trainer.user):
        if user is not None and (actor is None or user.pk != actor.pk):
            Notification.send(user, title, message, "/me/appointments/" if user.role == "member" else appt.get_absolute_url())


def _describe(appt):
    return f"{timezone.localtime(appt.start):%a %b %d, %H:%M} with {appt.trainer.full_name}"


@transaction.atomic
def book(member, trainer, start, duration, user, notes="", as_member=False, now=None):
    # Lock order is always trainer -> member so concurrent bookings cannot deadlock.
    trainer = Trainer.objects.select_for_update().get(pk=trainer.pk)
    member = Member.objects.select_related("user").select_for_update(of=("self",)).get(pk=member.pk)
    end = validate_slot(trainer, member, start, duration, as_member=as_member, now=now)
    try:
        with transaction.atomic():
            appt = Appointment.objects.create(
                member=member, trainer=trainer, start=start, end=end, fee=fee_for(trainer, duration), notes=notes[:255],
                booked_by=user, booked_via="portal" if as_member else "staff")
    except IntegrityError:
        raise BookingError("That time was just taken. Please choose another slot.")
    appt.member, appt.trainer = member, trainer
    _event(appt, "booked", user, f"{duration} min, fee LKR {appt.fee:,.2f}")
    ActivityLog.record(user, "appointment_booked", f"Booked {member.full_name}: {_describe(appt)}")
    _notify(appt, user, "Session booked", f"{member.full_name}: {_describe(appt)}")
    return appt


def _lock(appt):
    appt = Appointment.objects.select_related("member__user", "trainer__user").select_for_update(of=("self",)).get(pk=appt.pk)
    Trainer.objects.select_for_update().get(pk=appt.trainer_id)
    return appt


def _check_notice(appt, as_member, now):
    if as_member and appt.start - now < timedelta(hours=settings.APPOINTMENT_CHANGE_NOTICE_HOURS):
        raise BookingError(
            f"Changes need at least {settings.APPOINTMENT_CHANGE_NOTICE_HOURS} hours notice. Please contact the gym.")


@transaction.atomic
def reschedule(appt, new_start, user, as_member=False, now=None):
    """Move a scheduled session to another start time (same length). Any payment stays attached."""
    now = now or timezone.now()
    appt = _lock(appt)
    if appt.status != Status.SCHEDULED:
        raise BookingError("Only a scheduled session can be rescheduled.")
    _check_notice(appt, as_member, now)
    duration = appt.duration_minutes
    end = validate_slot(appt.trainer, appt.member, new_start, duration, as_member=as_member, exclude=appt, now=now)
    old = _describe(appt)
    appt.start, appt.end, appt.reminder_sent_at = new_start, end, None  # a new time re-arms the reminder
    try:
        with transaction.atomic():
            appt.save(update_fields=["start", "end", "reminder_sent_at", "updated_at"])
    except IntegrityError:
        raise BookingError("That time was just taken. Please choose another slot.")
    _event(appt, "rescheduled", user, f"from {old}")
    ActivityLog.record(user, "appointment_rescheduled", f"Rescheduled {appt.member.full_name}: {old} → {_describe(appt)}")
    _notify(appt, user, "Session rescheduled", f"Now {_describe(appt)} (was {old}).")
    return appt


@transaction.atomic
def cancel(appt, user, reason, as_member=False, force=False, now=None):
    now = now or timezone.now()
    appt = _lock(appt)
    if appt.status != Status.SCHEDULED:
        raise BookingError("Only a scheduled session can be cancelled.")
    if not force:
        _check_notice(appt, as_member, now)
    reason = (reason or "").strip()
    if not reason:
        raise BookingError("Please give a reason.")
    appt.status, appt.cancel_reason, appt.cancelled_by, appt.cancelled_at = Status.CANCELLED, reason[:255], user, now
    appt.save(update_fields=["status", "cancel_reason", "cancelled_by", "cancelled_at", "updated_at"])
    _event(appt, "cancelled", user, reason)
    ActivityLog.record(user, "appointment_cancelled", f"Cancelled {appt.member.full_name}: {_describe(appt)}")
    _notify(appt, user, "Session cancelled", f"{_describe(appt)} was cancelled. {reason}")
    return appt


@transaction.atomic
def finish(appt, user, outcome, now=None):
    """Record the outcome of a session that has started: completed or no_show."""
    now = now or timezone.now()
    if outcome not in (Status.COMPLETED, Status.NO_SHOW):
        raise BookingError("Invalid outcome.")
    appt = _lock(appt)
    if appt.status != Status.SCHEDULED:
        raise BookingError("This session already has an outcome.")
    if appt.start > now:
        raise BookingError("The session has not started yet.")
    appt.status = outcome
    appt.save(update_fields=["status", "updated_at"])
    _event(appt, outcome, user)
    ActivityLog.record(user, f"appointment_{outcome}", f"Marked {outcome.replace('_', ' ')}: {appt.member.full_name}, {_describe(appt)}")
    return appt


# --------------------------------------------------------------------------------------------- slots
def available_slots(trainer, day, duration, *, as_member=False, now=None):
    """Start times (aware datetimes) that are currently bookable on `day` (a local date)."""
    now = now or timezone.now()
    if duration not in settings.APPOINTMENT_DURATIONS or not trainer.is_bookable:
        return []
    if TrainerTimeOff.objects.filter(trainer=trainer, start_date__lte=day, end_date__gte=day).exists():
        return []
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(day, time.min), tz)
    taken = list(Appointment.objects.filter(trainer=trainer, status=Status.SCHEDULED, start__lt=lo + timedelta(days=1), end__gt=lo).values_list("start", "end"))
    earliest = now + timedelta(minutes=settings.APPOINTMENT_MIN_LEAD_MINUTES if as_member else 0)
    latest = now + timedelta(days=settings.APPOINTMENT_MAX_ADVANCE_DAYS)
    step = timedelta(minutes=settings.APPOINTMENT_SLOT_STEP_MINUTES)
    out = []
    for window in TrainerAvailability.objects.filter(trainer=trainer, weekday=day.weekday()):
        cursor = timezone.make_aware(datetime.combine(day, window.start_time), tz)
        limit = timezone.make_aware(datetime.combine(day, window.end_time), tz)
        while cursor + timedelta(minutes=duration) <= limit:
            end = cursor + timedelta(minutes=duration)
            if cursor > earliest and cursor <= latest and not any(s < end and e > cursor for s, e in taken):
                out.append(cursor)
            cursor += step
    return out


# ------------------------------------------------------------------------------------------ reminders
def send_due_reminders(now=None):
    """In-app reminder (plus e-mail/SMS/WhatsApp copies where configured and allowed) for sessions starting within the
    configured number of hours (Notification settings). Each session is reminded once; rescheduling re-arms it.
    External copies are queued and reported honestly by the notification delivery log, never assumed sent."""
    from apps.notifications.models import NotificationSettings

    now = now or timezone.now()
    hours = NotificationSettings.load().appointment_reminder_hours
    due = Appointment.objects.filter(
        status=Status.SCHEDULED, reminder_sent_at__isnull=True, start__gt=now, start__lte=now + timedelta(hours=hours),
    ).select_related("member__user", "trainer__user")
    count = 0
    for appt in due:
        stamp = int(appt.start.timestamp())
        text = f"Reminder: training session {_describe(appt)}."
        if appt.member.user_id:
            Notification.send(appt.member.user, "Session reminder", text, "/me/appointments/", kind="appointment-reminder",
                              dedupe_key=f"appointment-reminder:{appt.pk}:{stamp}", external=True)
        if appt.trainer.user_id:
            Notification.send(appt.trainer.user, "Session reminder", f"{appt.member.full_name}: {_describe(appt)}", appt.get_absolute_url(),
                              kind="appointment-reminder", dedupe_key=f"appointment-reminder:{appt.pk}:{stamp}")
        appt.reminder_sent_at = now
        appt.save(update_fields=["reminder_sent_at"])
        _event(appt, "reminder", None)
        count += 1
    return count

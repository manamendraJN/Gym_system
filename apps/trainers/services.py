from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import ActivityLog, Notification
from apps.members.models import Member

from .models import EmploymentStatus, Trainer, TrainerAssignment


class AssignmentError(Exception):
    """A rule prevented the operation; the message is safe to show to staff."""


@transaction.atomic
def assign_trainer(trainer, member, user, notes="", start_date=None):
    trainer = Trainer.objects.select_for_update().get(pk=trainer.pk)
    member = Member.objects.select_for_update().get(pk=member.pk)
    if trainer.employment_status == EmploymentStatus.TERMINATED:
        raise AssignmentError("This trainer has left the gym and cannot be assigned members.")
    if not member.is_active:
        raise AssignmentError("This member is deactivated.")
    if TrainerAssignment.objects.filter(trainer=trainer, member=member, end_date__isnull=True).exists():
        raise AssignmentError(f"{member.full_name} is already assigned to {trainer.full_name}.")
    try:
        with transaction.atomic():
            assignment = TrainerAssignment.objects.create(
                trainer=trainer, member=member, start_date=start_date or timezone.localdate(), notes=notes[:255], assigned_by=user)
    except IntegrityError:
        raise AssignmentError(f"{member.full_name} is already assigned to {trainer.full_name}.")
    ActivityLog.record(user, "trainer_assigned", f"Assigned {member.full_name} to trainer {trainer.full_name}")
    Notification.send(trainer.user, "New member assigned", f"{member.full_name} ({member.member_id}) was assigned to you.", member.get_absolute_url())
    if member.user_id:
        Notification.send(member.user, "Trainer assigned", f"{trainer.full_name} is now your trainer.", "/me/trainers/")
    return assignment


@transaction.atomic
def end_assignment(assignment, user):
    assignment = TrainerAssignment.objects.select_related("trainer", "member").select_for_update(of=("self",)).get(pk=assignment.pk)
    if assignment.end_date is not None:
        raise AssignmentError("This assignment has already ended.")
    assignment.end_date = max(timezone.localdate(), assignment.start_date)
    assignment.ended_by = user
    assignment.save(update_fields=["end_date", "ended_by"])
    ActivityLog.record(user, "trainer_unassigned", f"Ended assignment of {assignment.member.full_name} to {assignment.trainer.full_name}")
    return assignment


@transaction.atomic
def offboard_trainer(trainer, user):
    """Called when a trainer leaves: end their assignments and cancel their future sessions (history is kept)."""
    from apps.appointments import services as appointment_services
    from apps.appointments.models import Appointment, Status

    for assignment in trainer.assignments.filter(end_date__isnull=True):
        end_assignment(assignment, user)
    cancelled = 0
    for appt in Appointment.objects.filter(trainer=trainer, status=Status.SCHEDULED, start__gt=timezone.now()):
        appointment_services.cancel(appt, user, "The trainer is no longer available.", force=True)
        cancelled += 1
    return cancelled

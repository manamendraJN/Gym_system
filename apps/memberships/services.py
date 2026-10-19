"""Membership business rules. Every operation runs in a transaction and locks the member row, so two
staff members acting on the same member cannot create overlapping or inconsistent periods.

Rules
-----
* A period covers ``start_date`` .. ``end_date`` inclusive; expiry is derived from dates, no cron needed.
* A member can have at most one live (active/suspended) timeline without overlaps.
* New membership: starts today..today+MEMBERSHIP_MAX_ADVANCE_START_DAYS; not allowed while another live
  membership covers/overlaps that period (use renew or change instead).
* Renewal: allowed on an active (not suspended) membership; only one queued renewal at a time.
  - Before expiry (or on the last day): the new period starts the day after the current one ends, so no paid
    days are lost and the current period is left untouched.
  - After expiry: the new period starts today (no back-dating).
* Plan change (upgrade/downgrade): only on a currently covering membership. The old row is marked
  ``superseded`` (dates kept for history) and a new full period starts today at the new plan's price.
  No proration is applied; any price difference is settled by the payment module (Phase 3).
* Suspension: only on a currently covering membership. On reactivation the suspended days, capped at
  MEMBERSHIP_MAX_SUSPENSION_DAYS, are added to the end date and any queued renewal shifts by the same amount.
* Cancellation: allowed on active/suspended memberships (including scheduled ones) with a reason; access ends
  immediately, the row and its dates are kept. Any refund is handled by the payment module (Phase 3).
* The price is snapshotted onto the membership, so later plan price edits never change existing memberships.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Notification
from apps.members.models import Member

from .models import Membership, MembershipKind, MembershipStatus

A, S = MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED


class MembershipError(Exception):
    """A business rule prevented the operation; the message is safe to show to staff."""


def _lock(member_id):
    return Member.objects.select_for_update().get(pk=member_id)


def _fresh(membership_id):
    """Lock the member and reload the membership inside the transaction."""
    member_id = Membership.objects.values_list("member_id", flat=True).get(pk=membership_id)
    member = _lock(member_id)
    membership = Membership.objects.select_related("plan", "member").select_for_update().get(pk=membership_id)
    membership.member = member
    return member, membership


def _require_active_member(member):
    if not member.is_active:
        raise MembershipError("This member is deactivated. Reactivate the member first.")


def _require_active_plan(plan):
    if not plan.is_active:
        raise MembershipError(f"The plan “{plan.name}” is inactive and cannot be assigned.")


def _overlaps(member, start, end, exclude_pk=None):
    qs = Membership.objects.filter(member=member, status__in=[A, S], start_date__lte=end, end_date__gte=start)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _create(member, plan, start, kind, user, previous=None, notes=""):
    end = plan.end_date_for(start)
    if end < start:
        raise MembershipError("The calculated membership period is invalid.")
    if _overlaps(member, start, end):
        raise MembershipError("This period overlaps an existing membership for the member.")
    return Membership.objects.create(
        member=member, plan=plan, kind=kind, previous=previous, start_date=start, end_date=end,
        price=plan.price, created_by=user, notes=notes[:255],
    )


@transaction.atomic
def assign_membership(member, plan, start_date, user, notes=""):
    today = timezone.localdate()
    member = _lock(member.pk)
    _require_active_member(member)
    _require_active_plan(plan)
    if start_date < today:
        raise MembershipError("A membership cannot start in the past.")
    if start_date > today + timedelta(days=settings.MEMBERSHIP_MAX_ADVANCE_START_DAYS):
        raise MembershipError(f"A membership can start at most {settings.MEMBERSHIP_MAX_ADVANCE_START_DAYS} days ahead.")
    if Membership.objects.filter(member=member).filter(Q(status=A, end_date__gte=today) | Q(status=S)).exists():
        raise MembershipError("The member already has a current or scheduled membership. Renew or change it instead.")
    created = _create(member, plan, start_date, MembershipKind.NEW, user, notes=notes)
    Notification.send(member.user, "Membership started", f"Your {plan.name} membership runs {created.start_date:%b %d, %Y} – {created.end_date:%b %d, %Y}.",
                      "/me/", kind="membership-started", dedupe_key=f"membership-started:{created.pk}", external=True)
    return created


@transaction.atomic
def renew_membership(membership, user, plan=None, notes=""):
    today = timezone.localdate()
    member, current = _fresh(membership.pk)
    _require_active_member(member)
    if current.status == S:
        raise MembershipError("Reactivate the suspended membership before renewing it.")
    if current.status != A:
        raise MembershipError("Only an active or expired membership can be renewed.")
    plan = plan or current.plan
    _require_active_plan(plan)
    if Membership.objects.filter(member=member, status=A, start_date__gt=today).exists() or \
            current.followed_by.filter(status__in=[A, S]).exists():
        raise MembershipError("This membership has already been renewed.")
    start = current.end_date + timedelta(days=1) if current.end_date >= today else today
    renewed = _create(member, plan, start, MembershipKind.RENEWAL, user, previous=current, notes=notes)
    Notification.send(member.user, "Membership renewed", f"Your {plan.name} membership was renewed: {renewed.start_date:%b %d, %Y} – {renewed.end_date:%b %d, %Y}.",
                      "/me/", kind="membership-renewed", dedupe_key=f"membership-renewed:{renewed.pk}", external=True)
    return renewed


@transaction.atomic
def change_plan(membership, new_plan, user, notes=""):
    today = timezone.localdate()
    member, current = _fresh(membership.pk)
    _require_active_member(member)
    _require_active_plan(new_plan)
    if not current.is_covering:
        raise MembershipError("Only a currently active membership can be upgraded or downgraded.")
    if new_plan.pk == current.plan_id:
        raise MembershipError("Choose a different plan.")
    if Membership.objects.filter(member=member, status__in=[A, S], start_date__gt=today).exists():
        raise MembershipError("Cancel the scheduled renewal before changing the plan.")
    if new_plan.price > current.plan.price:
        kind = MembershipKind.UPGRADE
    elif new_plan.price < current.plan.price:
        kind = MembershipKind.DOWNGRADE
    else:
        kind = MembershipKind.CHANGE
    current.status = MembershipStatus.SUPERSEDED
    current.ended_on = today
    current.save(update_fields=["status", "ended_on", "updated_at"])
    return _create(member, new_plan, today, kind, user, previous=current, notes=notes)


@transaction.atomic
def suspend_membership(membership, user, reason):
    today = timezone.localdate()
    member, current = _fresh(membership.pk)
    if not current.is_covering:
        raise MembershipError("Only a currently active membership can be suspended.")
    if not reason.strip():
        raise MembershipError("A reason is required.")
    current.status = S
    current.suspended_on = today
    current.suspension_reason = reason.strip()[:255]
    current.save(update_fields=["status", "suspended_on", "suspension_reason", "updated_at"])
    return current


@transaction.atomic
def reactivate_membership(membership, user):
    today = timezone.localdate()
    member, current = _fresh(membership.pk)
    if current.status != S:
        raise MembershipError("This membership is not suspended.")
    _require_active_member(member)
    credited = min(max((today - current.suspended_on).days, 0), settings.MEMBERSHIP_MAX_SUSPENSION_DAYS)
    current.status = A
    current.end_date += timedelta(days=credited)
    current.suspended_days += credited
    current.suspended_on = None
    current.save(update_fields=["status", "end_date", "suspended_days", "suspended_on", "updated_at"])
    if credited:  # keep queued renewals contiguous with the extended period
        for later in Membership.objects.filter(previous=current, status__in=[A, S]).select_for_update():
            later.start_date += timedelta(days=credited)
            later.end_date += timedelta(days=credited)
            later.save(update_fields=["start_date", "end_date", "updated_at"])
    return current, credited


@transaction.atomic
def cancel_membership(membership, user, reason):
    today = timezone.localdate()
    member, current = _fresh(membership.pk)
    if current.status not in (A, S):
        raise MembershipError("This membership is already closed.")
    if current.status == A and current.end_date < today:
        raise MembershipError("An expired membership cannot be cancelled.")
    if not reason.strip():
        raise MembershipError("A reason is required.")
    if Membership.objects.filter(previous=current, status__in=[A, S]).exists():
        raise MembershipError("Cancel the scheduled renewal first.")
    current.status = MembershipStatus.CANCELLED
    current.ended_on = today
    current.suspended_on = None
    current.cancellation_reason = reason.strip()[:255]
    current.save(update_fields=["status", "ended_on", "suspended_on", "cancellation_reason", "updated_at"])
    return current

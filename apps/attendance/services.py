"""Check-in / check-out rules.

A check-in is accepted only when ALL of these hold:
  1. the member exists and is active;
  2. the member has a membership that covers today (active status, start <= today <= end) --
     suspended, expired, scheduled, cancelled or missing memberships are refused with the reason;
  3. the plan's access rules (allowed weekdays / opening hours, in local time) allow the current moment;
  4. (optional, ATTENDANCE_BLOCK_UNPAID) the membership has at least some payment recorded;
  5. the member is not already checked in (also enforced by a partial unique index).
An open visit older than ATTENDANCE_MAX_SESSION_HOURS is closed automatically (flagged) so a forgotten
check-out never locks a member out.
"""
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.members.models import Member
from apps.memberships.models import Membership, MembershipStatus

from .models import Attendance, Method

QR_PREFIX = "GYMQR:"


class CheckInDenied(Exception):
    """Access refused; the message is safe to show to staff."""


def qr_payload(member):
    """What is encoded in the QR image: an opaque token only, never personal data."""
    return f"{QR_PREFIX}{member.qr_token}"


def parse_qr(raw):
    raw = (raw or "").strip()
    return raw[len(QR_PREFIX):] if raw.startswith(QR_PREFIX) else raw


def covering_membership(member, today):
    return (
        Membership.objects.select_related("plan")
        .filter(member=member, status=MembershipStatus.ACTIVE, start_date__lte=today, end_date__gte=today)
        .first()
    )


def explain_no_membership(member, today):
    ms = Membership.objects.filter(member=member).exclude(status__in=["cancelled", "superseded"]).order_by("-end_date")
    if ms.filter(status=MembershipStatus.SUSPENDED).exists():
        return "The membership is suspended."
    upcoming = ms.filter(status=MembershipStatus.ACTIVE, start_date__gt=today).order_by("start_date").first()
    if upcoming:
        return f"The membership starts on {upcoming.start_date:%b %d, %Y}."
    last = ms.filter(status=MembershipStatus.ACTIVE).first()
    if last:
        return f"The membership expired on {last.end_date:%b %d, %Y}."
    return "The member has no active membership."


def evaluate_access(member, now=None):
    """Return the covering membership or raise CheckInDenied with the reason."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    today = local.date()
    if not member.is_active:
        raise CheckInDenied("This member account is deactivated.")
    membership = covering_membership(member, today)
    if membership is None:
        raise CheckInDenied(explain_no_membership(member, today))
    reason = membership.plan.access_denial(local)
    if reason:
        raise CheckInDenied(reason)
    if settings.ATTENDANCE_BLOCK_UNPAID:
        from apps.payments.billing import with_billing

        if with_billing(Membership.objects.filter(pk=membership.pk)).get().paid_total <= 0:
            raise CheckInDenied("No payment has been recorded for this membership.")
    return membership


def _close_stale(member, now):
    cutoff = now - timedelta(hours=settings.ATTENDANCE_MAX_SESSION_HOURS)
    for visit in Attendance.objects.select_for_update().filter(member=member, check_out__isnull=True, check_in__lt=cutoff):
        visit.check_out = visit.check_in + timedelta(hours=settings.ATTENDANCE_MAX_SESSION_HOURS)
        visit.auto_closed = True
        visit.save(update_fields=["check_out", "auto_closed"])


@transaction.atomic
def check_in(member, user, method=Method.MANUAL, now=None):
    now = now or timezone.now()
    member = Member.objects.select_for_update().get(pk=member.pk)  # serialises concurrent check-ins
    _close_stale(member, now)
    open_visit = Attendance.objects.filter(member=member, check_out__isnull=True).first()
    if open_visit:
        raise CheckInDenied(f"Already checked in since {timezone.localtime(open_visit.check_in):%H:%M}.")
    membership = evaluate_access(member, now)
    try:
        with transaction.atomic():
            return Attendance.objects.create(
                member=member, membership=membership, check_in=now, method=method, checked_in_by=user
            )
    except IntegrityError:  # lost a race with another terminal; the unique index caught the duplicate
        raise CheckInDenied("Already checked in.")


@transaction.atomic
def check_out(visit, user, now=None):
    now = now or timezone.now()
    visit = Attendance.objects.select_for_update().get(pk=visit.pk)
    if visit.check_out:
        raise CheckInDenied("This visit is already checked out.")
    visit.check_out = max(now, visit.check_in)
    visit.checked_out_by = user
    visit.save(update_fields=["check_out", "checked_out_by"])
    return visit


# ------------------------------------------------------------------------------------------ QR
def _fail_key(user):
    return f"qr-fail:{user.pk}"


def qr_scan_blocked(user):
    return cache.get(_fail_key(user), 0) >= settings.QR_SCAN_MAX_FAILURES


def scan_qr(raw, user, now=None):
    """Resolve a scanned code to a member and toggle: check out if inside, otherwise check in.
    Returns (action, attendance, member). Unknown codes count against a per-user failure limit."""
    if qr_scan_blocked(user):
        raise CheckInDenied("Too many unrecognised codes. Please wait a few minutes and try again.")
    token = parse_qr(raw)
    member = Member.objects.filter(qr_token=token).first() if 20 <= len(token) <= 64 else None
    if member is None:
        key = _fail_key(user)
        cache.add(key, 0, settings.QR_SCAN_WINDOW_SECONDS)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, settings.QR_SCAN_WINDOW_SECONDS)
        raise CheckInDenied("Unrecognised QR code.")
    open_visit = Attendance.objects.filter(member=member, check_out__isnull=True).first()
    if open_visit and open_visit.check_in >= (now or timezone.now()) - timedelta(hours=settings.ATTENDANCE_MAX_SESSION_HOURS):
        return "out", check_out(open_visit, user, now), member
    return "in", check_in(member, user, Method.QR, now), member

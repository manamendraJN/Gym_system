"""Reusable membership queries (shared by list pages, filters and the dashboard)."""
from datetime import timedelta

from django.conf import settings
from django.db.models import Case, CharField, Exists, OuterRef, Value, When
from django.utils import timezone

from .models import Membership, MembershipStatus

A, S = MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED

# Member-level status (precedence top to bottom).
MEMBER_STATUS_LABELS = {
    "inactive": "Inactive",
    "active": "Active",
    "suspended": "Suspended",
    "scheduled": "Scheduled",
    "expired": "Expired",
    "none": "No membership",
}


def members_with_status(queryset, today=None):
    """Annotate members with `membership_status` derived from their memberships."""
    today = today or timezone.localdate()
    own = Membership.objects.filter(member=OuterRef("pk"))
    covered = Exists(own.filter(status=A, start_date__lte=today, end_date__gte=today))
    suspended = Exists(own.filter(status=S))
    scheduled = Exists(own.filter(status=A, start_date__gt=today))
    expired = Exists(own.filter(status=A, end_date__lt=today))
    return queryset.annotate(
        membership_status=Case(
            When(is_active=False, then=Value("inactive")),
            When(covered, then=Value("active")),
            When(suspended, then=Value("suspended")),
            When(scheduled, then=Value("scheduled")),
            When(expired, then=Value("expired")),
            default=Value("none"),
            output_field=CharField(),
        )
    )


def _is_latest(queryset):
    """Keep memberships that no other open membership of the same member outlasts
    (i.e. not already renewed / followed by something later)."""
    later = Membership.objects.filter(
        member=OuterRef("member"), status__in=[A, S], end_date__gt=OuterRef("end_date")
    )
    return queryset.exclude(Exists(later))


def expired_memberships(today=None):
    today = today or timezone.localdate()
    qs = Membership.objects.filter(status=A, end_date__lt=today)
    return _is_latest(qs).select_related("member", "plan")


def expiring_soon(days=None, today=None):
    today = today or timezone.localdate()
    days = settings.MEMBERSHIP_EXPIRING_SOON_DAYS if days is None else days
    qs = Membership.objects.filter(
        status=A, start_date__lte=today, end_date__gte=today, end_date__lte=today + timedelta(days=days),
        member__is_active=True,
    )
    return _is_latest(qs).select_related("member", "plan")

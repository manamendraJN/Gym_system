import calendar
from datetime import date, timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone


def add_months(start, months):
    """Calendar-aware month addition; the day is clamped to the target month's length."""
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class DurationType(models.TextChoices):
    MONTHLY = "monthly", "Monthly (1 month)"
    QUARTERLY = "quarterly", "3 months"
    SEMIANNUAL = "semiannual", "6 months"
    ANNUAL = "annual", "Annual (12 months)"
    CUSTOM = "custom", "Custom (number of days)"


MONTHS_BY_TYPE = {
    DurationType.MONTHLY: 1,
    DurationType.QUARTERLY: 3,
    DurationType.SEMIANNUAL: 6,
    DurationType.ANNUAL: 12,
}


class MembershipPlan(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    duration_type = models.CharField(max_length=12, choices=DurationType.choices, default=DurationType.MONTHLY)
    custom_days = models.PositiveIntegerField(null=True, blank=True, help_text="Only for custom plans.")
    price = models.DecimalField("price (LKR)", max_digits=10, decimal_places=2)
    benefits = models.TextField(blank=True, help_text="One benefit per line.")
    access_restrictions = models.TextField(blank=True, help_text="Free-text note shown to staff (the rules below are what is enforced).")
    # Enforced at check-in. Blank / null means unrestricted.
    allowed_weekdays = models.CharField(max_length=13, blank=True, help_text="Comma-separated weekday numbers, 0=Monday.")
    access_from = models.TimeField(null=True, blank=True)
    access_until = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["price", "name", "pk"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="unique_plan_name_ci"),
            models.CheckConstraint(condition=Q(price__gte=0), name="plan_price_non_negative"),
            models.CheckConstraint(
                condition=(Q(duration_type="custom", custom_days__isnull=False, custom_days__gt=0) | (~Q(duration_type="custom") & Q(custom_days__isnull=True))),
                name="plan_custom_days_matches_type",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("memberships:plan_detail", args=[self.pk])

    @property
    def duration_label(self):
        if self.duration_type == DurationType.CUSTOM:
            return f"{self.custom_days} day{'s' if self.custom_days != 1 else ''}"
        months = MONTHS_BY_TYPE[self.duration_type]
        return "1 month" if months == 1 else f"{months} months"

    @property
    def weekday_numbers(self):
        return [int(d) for d in self.allowed_weekdays.split(",") if d.strip().isdigit()]

    @property
    def has_access_rules(self):
        return bool(self.allowed_weekdays or self.access_from)

    @property
    def access_summary(self):
        parts = []
        if self.allowed_weekdays:
            parts.append(", ".join(WEEKDAY_NAMES[d] for d in sorted(self.weekday_numbers)))
        if self.access_from and self.access_until:
            parts.append(f"{self.access_from:%H:%M}–{self.access_until:%H:%M}")
        return " · ".join(parts) or "Unrestricted"

    def access_denial(self, local_dt):
        """Reason string if `local_dt` (timezone-aware, local) is outside the plan's access rules, else None."""
        if self.allowed_weekdays and local_dt.weekday() not in self.weekday_numbers:
            return f"This plan only allows access on {self.access_summary.split(' · ')[0]}."
        if self.access_from and self.access_until and not (self.access_from <= local_dt.time() <= self.access_until):
            return f"This plan only allows access between {self.access_from:%H:%M} and {self.access_until:%H:%M}."
        return None

    @property
    def benefit_list(self):
        return [line.strip() for line in self.benefits.splitlines() if line.strip()]

    def end_date_for(self, start):
        """Last day covered (inclusive) when the plan starts on `start`.

        Month plans run to the day before the next anniversary (1 Jan + 1 month -> 31 Jan);
        custom plans cover exactly `custom_days` days including the start day.
        """
        if self.duration_type == DurationType.CUSTOM:
            return start + timedelta(days=self.custom_days - 1)
        return add_months(start, MONTHS_BY_TYPE[self.duration_type]) - timedelta(days=1)


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    CANCELLED = "cancelled", "Cancelled"
    SUPERSEDED = "superseded", "Replaced by plan change"


class MembershipKind(models.TextChoices):
    NEW = "new", "New"
    RENEWAL = "renewal", "Renewal"
    UPGRADE = "upgrade", "Upgrade"
    DOWNGRADE = "downgrade", "Downgrade"
    CHANGE = "change", "Plan change"


# Derived (display) states in addition to the stored ones.
SCHEDULED, EXPIRED, CURRENT = "scheduled", "expired", "active"


class Membership(models.Model):
    """One membership period. Rows are never rewritten to represent a later period: renewals and plan
    changes create new rows, so the full history is preserved. Only the lifecycle fields
    (status, suspension and end bookkeeping) change on an existing row."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="memberships")
    plan = models.ForeignKey(MembershipPlan, on_delete=models.PROTECT, related_name="memberships")
    kind = models.CharField(max_length=10, choices=MembershipKind.choices, default=MembershipKind.NEW)
    previous = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="followed_by",
        help_text="The membership this one renews or replaces.",
    )
    start_date = models.DateField()
    end_date = models.DateField(help_text="Last covered day (inclusive).")
    # Snapshot of the price at purchase time: later plan price changes never alter existing memberships.
    price = models.DecimalField("price (LKR)", max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE, db_index=True)

    suspended_on = models.DateField(null=True, blank=True)
    suspension_reason = models.CharField(max_length=255, blank=True)
    suspended_days = models.PositiveIntegerField(default=0, help_text="Total days credited back to the end date.")
    ended_on = models.DateField(null=True, blank=True, help_text="Date it was cancelled or replaced.")
    cancellation_reason = models.CharField(max_length=255, blank=True)

    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [models.Index(fields=["member", "status", "end_date"])]
        constraints = [
            models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="membership_end_after_start"),
            models.CheckConstraint(condition=Q(price__gte=0), name="membership_price_non_negative"),
            models.CheckConstraint(
                condition=Q(status="suspended", suspended_on__isnull=False) | ~Q(status="suspended"),
                name="membership_suspended_has_date",
            ),
        ]

    def __str__(self):
        return f"{self.member} – {self.plan} ({self.start_date} → {self.end_date})"

    def get_absolute_url(self):
        return reverse("memberships:detail", args=[self.pk])

    def display_status(self, today=None):
        """Single key used for badges/filters: active, scheduled, expired, suspended, cancelled, superseded."""
        today = today or timezone.localdate()
        if self.status != MembershipStatus.ACTIVE:
            return self.status
        if self.start_date > today:
            return SCHEDULED
        if self.end_date < today:
            return EXPIRED
        return CURRENT

    @property
    def status_key(self):
        return self.display_status()

    @property
    def status_label(self):
        return STATUS_LABELS[self.status_key]

    @property
    def is_covering(self):
        """True when the member is entitled to access today."""
        return self.status_key == CURRENT

    @property
    def days_remaining(self):
        return (self.end_date - timezone.localdate()).days

    @property
    def is_open(self):
        """Still part of the member's live timeline (not cancelled/replaced)."""
        return self.status in (MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED)


STATUS_LABELS = {
    CURRENT: "Active",
    SCHEDULED: "Scheduled",
    EXPIRED: "Expired",
    "suspended": "Suspended",
    "cancelled": "Cancelled",
    "superseded": "Replaced",
}

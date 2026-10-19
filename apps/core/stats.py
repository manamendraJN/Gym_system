"""Database-driven dashboard statistics (nothing is hard-coded or faked).

Definitions
* Revenue collected = completed payments minus completed refunds, by payment date. Pending, failed and voided
  entries are never revenue, and neither is money that is merely owed.
* Outstanding = unpaid membership fees and personal-training fees (what members still owe on non-cancelled items). This is a receivable,
  shown separately from revenue.
"""
from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Case, Count, DecimalField, F, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.appointments.models import Appointment
from apps.appointments.models import Status as AppointmentStatus
from apps.attendance.models import Attendance
from apps.core.models import ActivityLog
from apps.members.models import Member
from apps.memberships.models import Membership, MembershipStatus
from apps.memberships.queries import expiring_soon, members_with_status
from apps.payments.billing import PAYMENT_STATUS_LABELS, with_billing
from apps.payments.models import Kind, Payment, Status
from apps.trainers.access import scope_appointments, scope_by_member
from apps.trainers.models import EmploymentStatus, Trainer
from apps.workouts.models import PlanStatus, WorkoutPlan

ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=14, decimal_places=2)

from apps.reports.finance import NET_AMOUNT, collected  # noqa: E402,F401  (single definition of revenue)


def get_dashboard_stats(today=None, user=None):
    today = today or timezone.localdate()
    month_start = today.replace(day=1)

    # --- members & memberships
    counts = {
        row["membership_status"]: row["n"]
        for row in members_with_status(Member.objects.all(), today).values("membership_status").annotate(n=Count("pk"))
    }
    window = settings.MEMBERSHIP_EXPIRING_SOON_DAYS
    upcoming = list(expiring_soon(window, today).order_by("end_date")[:8])

    # --- attendance
    visits = Attendance.objects
    todays_attendance = visits.filter(check_in__date=today).count()
    first_day = today - timedelta(days=6)
    per_day = dict(
        visits.filter(check_in__date__gte=first_day, check_in__date__lte=today)
        .annotate(d=TruncDate("check_in")).values_list("d").annotate(n=Count("pk"))
    )
    days = [first_day + timedelta(days=i) for i in range(7)]
    attendance_chart = {"labels": [d.strftime("%a %d") for d in days], "values": [per_day.get(d, 0) for d in days]}

    # --- payments
    ledger = Payment.objects
    first_month = (month_start - timedelta(days=150)).replace(day=1)
    rows = (
        ledger.filter(status=Status.COMPLETED, paid_on__gte=first_month, paid_on__lte=today).exclude(kind=Kind.ADJUSTMENT)
        .annotate(m=TruncMonth("paid_on")).values("m").annotate(total=NET_AMOUNT).order_by("m")
    )
    revenue_chart = {"labels": [r["m"].strftime("%b %Y") for r in rows], "values": [float(r["total"]) for r in rows]}

    billed = with_billing(Membership.objects.exclude(status=MembershipStatus.CANCELLED))
    # (Django cannot aggregate over an aggregate annotation, so the per-membership balances are summed here.)
    outstanding = sum((row["outstanding"] for row in billed.filter(outstanding__gt=0).values("outstanding")), ZERO)
    # Unpaid personal-training fees are receivables too.
    appt_billed = with_billing(Appointment.objects.exclude(status=AppointmentStatus.CANCELLED), "fee")
    outstanding += sum((row["outstanding"] for row in appt_billed.filter(outstanding__gt=0).values("outstanding")), ZERO)
    live = billed.filter(status__in=[MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED], end_date__gte=today)
    status_counts = Counter(live.values_list("payment_status", flat=True))
    pending_total = ledger.filter(kind=Kind.PAYMENT, status=Status.PENDING).aggregate(t=Coalesce(Sum("amount"), Value(ZERO), output_field=MONEY))["t"]

    stats = {
        "total_members": sum(counts.values()),
        "active_members": counts.get("active", 0),
        "expired_members": counts.get("expired", 0),
        "new_members": Member.objects.filter(registration_date__gte=month_start).count(),
        "todays_attendance": todays_attendance,
        "todays_revenue": collected(ledger.filter(paid_on=today)),
        "monthly_revenue": collected(ledger.filter(paid_on__gte=month_start, paid_on__lte=today)),
        "outstanding": outstanding,
    }

    # --- training (scoped: a trainer only sees their own sessions and assigned members' plans)
    appointments = Appointment.objects
    plans = WorkoutPlan.objects.filter(status=PlanStatus.ACTIVE)
    if user is not None:
        appointments = scope_appointments(user, appointments.all())
        plans = scope_by_member(user, plans)
    training = {
        "total_trainers": Trainer.objects.filter(employment_status=EmploymentStatus.ACTIVE).count(),
        "upcoming_appointments": appointments.filter(status=AppointmentStatus.SCHEDULED, start__gt=timezone.now()).count(),
        "completed_sessions": appointments.filter(status=AppointmentStatus.COMPLETED).count(),
        "plans_assigned": plans.count(),
    }

    role_counts = dict(User.objects.filter(is_active=True).values_list("role").annotate(n=Count("id")))
    role_chart = {
        "labels": [label for value, label in Role.choices if role_counts.get(value)],
        "values": [role_counts[value] for value, _ in Role.choices if role_counts.get(value)],
    }

    from apps.trainers.access import is_trainer_role

    ops = operations_stats(user, today, month_start)
    return {
        **ops,
        "stats": stats,
        "training": training,
        "scope_note": "Your own sessions / assigned members" if user is not None and is_trainer_role(user) else "",
        "upcoming_expirations": upcoming,
        "expiry_window": window,
        "revenue_chart": revenue_chart,
        "attendance_chart": attendance_chart,
        "inside_now": visits.filter(check_out__isnull=True).count(),
        "payment_breakdown": [
            {"key": key, "label": PAYMENT_STATUS_LABELS[key], "count": status_counts.get(key, 0)}
            for key in ("unpaid", "partial", "paid")
        ],
        "pending_total": pending_total,
        "recent_payments": ledger.exclude(kind=Kind.ADJUSTMENT).select_related("member", "membership__plan")[:8],
        "role_chart": role_chart,
        "recent_activities": ActivityLog.objects.select_related("user")[:8],
        "user_counts": {
            "active": User.objects.filter(is_active=True).count(),
            "inactive": User.objects.filter(is_active=False).count(),
            "staff": User.objects.exclude(role=Role.MEMBER).filter(is_active=True).count(),
        },
    }


def operations_stats(user, today, month_start):
    """Equipment, inventory, expense and announcement figures. Computed only for users allowed to see them."""
    from django.conf import settings as dj

    from apps.announcements.services import visible_for
    from apps.equipment.models import Equipment, EquipmentStatus
    from apps.expenses.models import Expense
    from apps.inventory.models import InventoryItem
    from apps.reports import finance

    out = {"announcements": list(visible_for(user)[:3]) if user is not None else []}
    can = (lambda code: user is not None and user.has_gym_perm(code))
    if can("equipment.view"):
        counts = {r["status"]: r["n"] for r in Equipment.objects.values("status").annotate(n=Count("pk"))}
        due = Equipment.objects.filter(next_maintenance_date__isnull=False, next_maintenance_date__lte=today + timedelta(days=14)).order_by("next_maintenance_date")
        out["equipment"] = {
            "counts": [{"key": k, "label": label, "n": counts.get(k, 0)} for k, label in EquipmentStatus.choices],
            "total": sum(counts.values()), "due": list(due[:5]), "due_count": due.count(),
        }
    if dj.INVENTORY_ENABLED and can("inventory.view"):
        low = [i for i in InventoryItem.objects.filter(is_active=True) if i.is_low]
        out["inventory"] = {"low_count": len(low), "low": low[:5]}
    if can("expenses.view"):
        spent = finance.expenses_total(month_start, today)
        by_cat = list(Expense.objects.filter(voided_at__isnull=True, expense_date__gte=month_start, expense_date__lte=today)
                      .values("category__name").annotate(t=Sum("amount")).order_by("-t"))
        out["expense_stats"] = {"month": spent, "chart": {"labels": [c["category__name"] for c in by_cat], "values": [float(c["t"]) for c in by_cat]}}
        if can("payments.view"):
            first = (month_start - timedelta(days=150)).replace(day=1)
            series = finance.monthly_series(first, today)
            revenue = finance.totals(month_start, today)["net"]
            out["expense_stats"].update(
                profit=revenue - spent, revenue=revenue,
                trend={"labels": [r["label"] for r in series], "revenue": [float(r["revenue"]) for r in series], "expenses": [float(r["expenses"]) for r in series]})
    return out

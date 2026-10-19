"""Financial rules used by every report and the dashboard (cash basis, nothing counted twice).

Definitions
-----------
* Invoiced      what members were billed in a period: the price of memberships that START in the period plus the fee of
                training sessions that take place in it, plus completed adjustments recorded in it. Cancelled memberships
                and sessions are not invoiced (cancelling writes off what was unpaid).
* Collected     completed PAYMENTS by payment date (gross). Pending, failed and voided entries never count.
* Refunds       completed REFUNDS by payment date.
* Revenue       collected minus refunds ("net collected"). Each payment is one ledger row, so it is counted exactly once;
                a membership or session fee is never added on top of the payments made for it.
* Outstanding   what members still owe NOW on non-cancelled memberships and sessions (invoiced minus net collected, never
                below zero). It is a receivable, not revenue, and does not depend on the report period.
* Expenses      non-void rows of the Expense table by expense date. Maintenance records and stock purchases create ONE
                linked expense each; their own cost fields are informational and are never added again.
* Estimated operating profit = revenue (net collected) - expenses for the same period. It is a cash-basis estimate: it
                ignores unpaid fees, equipment purchase cost unless recorded as an expense, depreciation, tax, and trainer
                compensation records unless they were entered as Salaries expenses.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Sum, Value, When
from django.db.models.functions import Coalesce, TruncMonth

from apps.expenses.models import Expense
from apps.payments.models import Kind, Payment, Status

ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=14, decimal_places=2)

NET_AMOUNT = Sum(Case(When(kind=Kind.PAYMENT, then=F("amount")), When(kind=Kind.REFUND, then=-F("amount")), default=Value(ZERO), output_field=MONEY))


def ledger(d_from=None, d_to=None):
    """Completed payments and refunds (never adjustments) within the period."""
    qs = Payment.objects.filter(status=Status.COMPLETED).exclude(kind=Kind.ADJUSTMENT)
    if d_from:
        qs = qs.filter(paid_on__gte=d_from)
    if d_to:
        qs = qs.filter(paid_on__lte=d_to)
    return qs


def _sum(qs, field="amount"):
    return qs.aggregate(t=Coalesce(Sum(field), Value(ZERO), output_field=MONEY))["t"]


def collected(qs):
    """Net revenue of a ledger queryset: payments minus refunds."""
    return qs.filter(status=Status.COMPLETED).exclude(kind=Kind.ADJUSTMENT).aggregate(t=Coalesce(NET_AMOUNT, Value(ZERO), output_field=MONEY))["t"]


def totals(d_from, d_to):
    base = ledger(d_from, d_to)
    gross, refunds = _sum(base.filter(kind=Kind.PAYMENT)), _sum(base.filter(kind=Kind.REFUND))
    return {"gross": gross, "refunds": refunds, "net": gross - refunds}


def expenses_total(d_from=None, d_to=None):
    qs = Expense.objects.filter(voided_at__isnull=True)
    if d_from:
        qs = qs.filter(expense_date__gte=d_from)
    if d_to:
        qs = qs.filter(expense_date__lte=d_to)
    return _sum(qs)


def invoiced(d_from, d_to):
    from apps.appointments.models import Appointment
    from apps.memberships.models import Membership

    memberships = Membership.objects.filter(start_date__gte=d_from, start_date__lte=d_to).exclude(status="cancelled")
    sessions = Appointment.objects.filter(start__date__gte=d_from, start__date__lte=d_to).exclude(status="cancelled")
    adjustments = Payment.objects.filter(kind=Kind.ADJUSTMENT, status=Status.COMPLETED, paid_on__gte=d_from, paid_on__lte=d_to)
    return _sum(memberships, "price") + _sum(sessions, "fee") + _sum(adjustments)


def outstanding_now():
    from apps.appointments.models import Appointment
    from apps.memberships.models import Membership
    from apps.payments.billing import with_billing

    rows = list(with_billing(Membership.objects.exclude(status="cancelled")).filter(outstanding__gt=0).values_list("outstanding", flat=True))
    rows += list(with_billing(Appointment.objects.exclude(status="cancelled"), "fee").filter(outstanding__gt=0).values_list("outstanding", flat=True))
    return sum(rows, ZERO)


def summary(d_from, d_to):
    t = totals(d_from, d_to)
    spent = expenses_total(d_from, d_to)
    return {**t, "invoiced": invoiced(d_from, d_to), "outstanding": outstanding_now(), "expenses": spent, "profit": t["net"] - spent}


def month_start(day):
    return day.replace(day=1)


def monthly_series(d_from, d_to):
    """One dict per calendar month between the dates: revenue (net collected), expenses and estimated profit."""
    start = month_start(d_from)
    net = {r["m"]: r["t"] for r in ledger(start, d_to).annotate(m=TruncMonth("paid_on")).values("m").annotate(t=NET_AMOUNT)}
    spent = {r["m"]: r["t"] for r in Expense.objects.filter(voided_at__isnull=True, expense_date__gte=start, expense_date__lte=d_to)
             .annotate(m=TruncMonth("expense_date")).values("m").annotate(t=Sum("amount"))}
    out, cursor = [], start
    while cursor <= d_to:
        revenue, cost = net.get(cursor, ZERO), spent.get(cursor, ZERO)
        out.append({"month": cursor, "label": cursor.strftime("%b %Y"), "revenue": revenue, "expenses": cost, "profit": revenue - cost})
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return out

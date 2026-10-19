"""The one place balances are calculated (used by lists, detail pages, receipts, services and the dashboard).

For a membership:
    amount_due   = membership.price + sum(completed adjustments)          (adjustments may be negative)
    net_paid     = sum(completed payments) - sum(completed refunds)
    outstanding  = max(amount_due - net_paid, 0)                          (0 for cancelled memberships: cancelling
                                                                          writes off what was still unpaid)
    pending      = sum(pending payments)  -- not yet counted anywhere until confirmed
Only COMPLETED entries count; pending, failed and voided entries never affect a balance.

payment_status:
    refunded / partially_refunded  money has been returned (net_paid == 0 / > 0)
    unpaid | partial | paid        by comparing net_paid with amount_due
"""
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest

from .models import Kind, Status

ZERO = Decimal("0.00")
MONEY = DecimalField(max_digits=14, decimal_places=2)

PAYMENT_STATUS_LABELS = {
    "unpaid": "Unpaid",
    "partial": "Partially paid",
    "paid": "Paid",
    "partially_refunded": "Partially refunded",
    "refunded": "Refunded",
}


def _sum(kind, status=Status.COMPLETED):
    return Coalesce(Sum("payments__amount", filter=Q(payments__kind=kind, payments__status=status)), Value(ZERO), output_field=MONEY)


def with_billing(queryset, price_field="price"):
    """Annotate memberships with paid_total, refund_total, adjustment_total, pending_total, amount_due, net_paid,
    outstanding and payment_status."""
    qs = queryset.annotate(
        paid_total=_sum(Kind.PAYMENT),
        refund_total=_sum(Kind.REFUND),
        adjustment_total=_sum(Kind.ADJUSTMENT),
        pending_total=_sum(Kind.PAYMENT, Status.PENDING),
    ).annotate(
        amount_due=F(price_field) + F("adjustment_total"),
        net_paid=F("paid_total") - F("refund_total"),
    )
    return qs.annotate(
        outstanding=Case(
            When(status="cancelled", then=Value(ZERO)),
            default=Greatest(F("amount_due") - F("net_paid"), Value(ZERO), output_field=MONEY),
            output_field=MONEY,
        ),
        payment_status=Case(
            When(Q(refund_total__gt=0) & Q(net_paid__lte=0), then=Value("refunded")),
            When(Q(refund_total__gt=0) & Q(net_paid__gt=0), then=Value("partially_refunded")),
            When(amount_due__lte=0, then=Value("paid")),
            When(net_paid__lte=0, then=Value("unpaid")),
            When(net_paid__lt=F("amount_due"), then=Value("partial")),
            default=Value("paid"),
        ),
    )


def appointment_billing(appointment):
    from apps.appointments.models import Appointment

    return with_billing(Appointment.objects.filter(pk=appointment.pk).select_related("member", "trainer"), "fee").get()


def billing_for(target):
    """Annotated billing figures for a Membership or an Appointment."""
    from apps.appointments.models import Appointment

    return appointment_billing(target) if isinstance(target, Appointment) else membership_billing(target)


def membership_billing(membership):
    """Annotated copy of a single membership."""
    from apps.memberships.models import Membership

    return with_billing(Membership.objects.filter(pk=membership.pk).select_related("member", "plan")).get()


def balance_as_of(payment):
    """Receipt figures: the membership's position immediately after this ledger entry (entries up to its id)."""
    from .models import Payment

    entries = Payment.objects.filter(status=Status.COMPLETED, pk__lte=payment.pk)
    entries = entries.filter(membership_id=payment.membership_id) if payment.membership_id else entries.filter(appointment_id=payment.appointment_id)

    def total(kind):
        return entries.filter(kind=kind).aggregate(t=Sum("amount"))["t"] or ZERO

    base = payment.membership.price if payment.membership_id else payment.appointment.fee
    due = base + total(Kind.ADJUSTMENT)
    net = total(Kind.PAYMENT) - total(Kind.REFUND)
    return {"amount_due": due, "net_paid": net, "outstanding": max(due - net, ZERO)}

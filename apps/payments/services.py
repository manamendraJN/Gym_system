"""Financial operations. Each one runs in a transaction and locks the membership row first, so concurrent
submissions for the same membership are processed one after another against up-to-date balances
(row locks apply on PostgreSQL; SQLite serialises writers). Every change writes a PaymentAudit row.

Double submissions: every form carries a one-time ``idempotency_key``; resubmitting the same key returns the
original entry instead of creating another (backed by a unique constraint, so a race cannot slip through).
"""
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import ActivityLog, Notification
from apps.appointments.models import Appointment
from apps.appointments.models import Status as AppointmentStatus
from apps.memberships.models import Membership, MembershipStatus

from .billing import ZERO, billing_for
from .models import Kind, Method, Payment, PaymentAudit, Status

CENT = Decimal("0.01")


class PaymentError(Exception):
    """A financial rule prevented the operation; the message is safe to show to staff."""


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def looks_like_card_number(text):
    compact = re.sub(r"[ -]", "", text or "")
    return compact.isdigit() and 13 <= len(compact) <= 19 and _luhn(compact)


def _audit(payment, action, user, old="", note=""):
    PaymentAudit.objects.create(
        payment=payment, action=action, old_status=old, new_status=payment.status, amount=payment.amount,
        user=user, note=note[:255],
    )
    ActivityLog.record(
        user, f"payment_{action}",
        f"{action.replace('_', ' ').capitalize()}: {payment.receipt_number} · LKR {payment.amount:,.2f} · {payment.member.full_name}",
    )


def _notify_member(payment, confirmed=False):
    """Tell the member (if they have a portal login) that a payment was recorded / confirmed. One per payment."""
    user = payment.member.user
    if user is None:
        return
    pending = payment.status == Status.PENDING
    if confirmed:
        title, text, key = "Payment confirmed", f"Your payment of LKR {payment.amount:,.2f} ({payment.receipt_number}) was confirmed.", f"payment-confirmed:{payment.pk}"
    elif pending:
        title, text, key = "Payment awaiting confirmation", f"We recorded your payment of LKR {payment.amount:,.2f} ({payment.receipt_number}); it counts once confirmed.", f"payment-recorded:{payment.pk}"
    else:
        title, text, key = "Payment received", f"Thank you – we recorded your payment of LKR {payment.amount:,.2f} ({payment.receipt_number}).", f"payment-recorded:{payment.pk}"
    Notification.send(user, title, text, "/me/", kind="payment-recorded", dedupe_key=key, external=True)


def _lock_membership(target):
    """Lock the item being paid for (a Membership or an Appointment)."""
    return type(target).objects.select_for_update().select_related("member").get(pk=target.pk)


def _fields(target):
    """Payment foreign key for the target."""
    return {"appointment": target} if isinstance(target, Appointment) else {"membership": target}


def _is_closed(target):
    if isinstance(target, Appointment):
        return target.status == AppointmentStatus.CANCELLED
    return target.status == MembershipStatus.CANCELLED


def _noun(target):
    return "appointment" if isinstance(target, Appointment) else "membership"


def _existing(key, membership=None):
    existing = Payment.objects.filter(idempotency_key=key).select_related("member").first()
    if existing and membership is not None and existing.target != membership:
        raise PaymentError("This form was already used for a different record. Reload the page and try again.")
    return existing


def _create(**fields):
    try:
        with transaction.atomic():
            return Payment.objects.create(**fields), True
    except IntegrityError:
        existing = Payment.objects.filter(idempotency_key=fields["idempotency_key"]).first()
        if existing is None:
            raise
        return existing, False


def _validate_amount(amount):
    amount = money(amount)
    if amount <= 0:
        raise PaymentError("The amount must be greater than zero.")
    if amount > Decimal("99999999.99"):
        raise PaymentError("The amount is too large.")
    return amount


# ---------------------------------------------------------------------------------------- payments
@transaction.atomic
def record_payment(membership, amount, method, user, idempotency_key, paid_on=None, external_reference="",
                   notes="", pending=False):
    """Record money received. Returns (payment, created)."""
    existing = _existing(idempotency_key, membership)
    if existing:
        return existing, False
    m = _lock_membership(membership)
    amount = _validate_amount(amount)
    today = timezone.localdate()
    paid_on = paid_on or today
    if method not in (Method.CASH, Method.BANK, Method.CARD):
        raise PaymentError("Choose a payment method.")
    if _is_closed(m):
        raise PaymentError(f"This {_noun(m)} was cancelled; payments can no longer be taken for it.")
    if paid_on > today:
        raise PaymentError("The payment date cannot be in the future.")
    if paid_on < today - timedelta(days=settings.PAYMENT_MAX_BACKDATE_DAYS):
        raise PaymentError(f"The payment date cannot be more than {settings.PAYMENT_MAX_BACKDATE_DAYS} days ago.")
    if looks_like_card_number(external_reference):
        raise PaymentError("Do not enter card numbers. Use the terminal's approval or slip number as the reference.")
    if pending and method == Method.CASH:
        raise PaymentError("Cash payments are collected immediately and cannot be pending.")
    bal = billing_for(m)
    available = bal.outstanding - bal.pending_total
    if amount > available:
        raise PaymentError(
            f"The amount exceeds the balance still payable (LKR {max(available, ZERO):,.2f}"
            + (f", after LKR {bal.pending_total:,.2f} awaiting confirmation" if bal.pending_total else "") + ")."
        )
    payment, created = _create(
        **_fields(m), member=m.member, kind=Kind.PAYMENT, amount=amount, method=method, paid_on=paid_on,
        status=Status.PENDING if pending else Status.COMPLETED, external_reference=external_reference.strip()[:60],
        notes=notes.strip()[:255], recorded_by=user, idempotency_key=idempotency_key,
    )
    if created:
        _audit(payment, "recorded", user, note=notes)
        _notify_member(payment)
    return payment, created


@transaction.atomic
def confirm_payment(payment, user):
    m = _lock_membership(payment.target)
    payment = Payment.objects.select_for_update().select_related("member").get(pk=payment.pk)
    if payment.status != Status.PENDING:
        raise PaymentError("Only a pending payment can be confirmed.")
    bal = billing_for(m)
    if payment.amount > bal.outstanding:
        raise PaymentError(f"Confirming this payment would exceed the balance due on the {_noun(m)}.")
    payment.status = Status.COMPLETED
    payment.save(update_fields=["status", "updated_at"])
    _audit(payment, "confirmed", user, old=Status.PENDING)
    _notify_member(payment, confirmed=True)
    return payment


@transaction.atomic
def reject_payment(payment, user, reason):
    _lock_membership(payment.target)
    payment = Payment.objects.select_for_update().select_related("member").get(pk=payment.pk)
    if payment.status != Status.PENDING:
        raise PaymentError("Only a pending payment can be rejected.")
    payment.status = Status.FAILED
    payment.save(update_fields=["status", "updated_at"])
    _audit(payment, "rejected", user, old=Status.PENDING, note=reason)
    return payment


@transaction.atomic
def void_payment(payment, user, reason):
    """Cancel a mistaken payment or refund entry. The row stays (status=voided) and is excluded from balances."""
    if not reason.strip():
        raise PaymentError("A reason is required.")
    _lock_membership(payment.target)
    payment = Payment.objects.select_for_update().select_related("member").get(pk=payment.pk)
    if payment.kind == Kind.ADJUSTMENT:
        raise PaymentError("Adjustments cannot be voided; record an opposite adjustment instead.")
    if payment.status not in (Status.COMPLETED, Status.PENDING):
        raise PaymentError("This entry cannot be voided.")
    if payment.kind == Kind.PAYMENT and payment.refunds.filter(status=Status.COMPLETED).exists():
        raise PaymentError("This payment has refunds against it and cannot be voided.")
    old = payment.status
    payment.status = Status.VOIDED
    payment.save(update_fields=["status", "updated_at"])
    _audit(payment, "voided", user, old=old, note=reason)
    return payment


# ------------------------------------------------------------------------------- refunds, adjustments
@transaction.atomic
def refund_payment(payment, amount, method, user, idempotency_key, reason, external_reference=""):
    existing = _existing(idempotency_key, payment.target)
    if existing:
        return existing, False
    m = _lock_membership(payment.target)
    original = Payment.objects.select_for_update().select_related("member").get(pk=payment.pk)
    amount = _validate_amount(amount)
    if not reason.strip():
        raise PaymentError("A reason is required for refunds.")
    if original.kind != Kind.PAYMENT or original.status != Status.COMPLETED:
        raise PaymentError("Only a completed payment can be refunded.")
    if method not in (Method.CASH, Method.BANK, Method.CARD):
        raise PaymentError("Choose a refund method.")
    if looks_like_card_number(external_reference):
        raise PaymentError("Do not enter card numbers.")
    already = sum((r.amount for r in original.refunds.filter(status=Status.COMPLETED)), ZERO)
    refundable = original.amount - already
    if amount > refundable:
        raise PaymentError(f"At most LKR {refundable:,.2f} can still be refunded on this payment.")
    refund, created = _create(
        **_fields(m), member=m.member, kind=Kind.REFUND, refund_of=original, amount=amount, method=method,
        status=Status.COMPLETED, reason=reason.strip()[:255], external_reference=external_reference.strip()[:60],
        recorded_by=user, idempotency_key=idempotency_key,
    )
    if created:
        _audit(refund, "refunded", user, note=f"of {original.receipt_number}: {reason}")
    return refund, created


@transaction.atomic
def adjust_charge(membership, amount, user, idempotency_key, reason):
    """Change what the member owes by `amount` (negative = discount/waiver, positive = surcharge)."""
    existing = _existing(idempotency_key, membership)
    if existing:
        return existing, False
    m = _lock_membership(membership)
    amount = money(amount)
    if amount == 0:
        raise PaymentError("The adjustment cannot be zero.")
    if not reason.strip():
        raise PaymentError("A reason is required for adjustments.")
    if _is_closed(m):
        raise PaymentError(f"This {_noun(m)} was cancelled and can no longer be adjusted.")
    bal = billing_for(m)
    new_due = bal.amount_due + amount
    if new_due < 0:
        raise PaymentError("The adjustment would make the amount due negative.")
    if new_due < bal.net_paid:
        raise PaymentError("The member has already paid more than the adjusted amount; refund the difference first.")
    adj, created = _create(
        **_fields(m), member=m.member, kind=Kind.ADJUSTMENT, amount=amount, method=Method.NONE, status=Status.COMPLETED,
        reason=reason.strip()[:255], recorded_by=user, idempotency_key=idempotency_key,
    )
    if created:
        _audit(adj, "adjusted", user, note=reason)
    return adj, created

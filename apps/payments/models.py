import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

TXN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters


def new_reference():
    """Unique, unguessable transaction reference, e.g. TXN-20261018-7KQ4M2XH."""
    suffix = "".join(secrets.choice(TXN_ALPHABET) for _ in range(8))
    return f"TXN-{timezone.localdate():%Y%m%d}-{suffix}"


class Kind(models.TextChoices):
    PAYMENT = "payment", "Payment"
    REFUND = "refund", "Refund"
    ADJUSTMENT = "adjustment", "Adjustment"


class Method(models.TextChoices):
    CASH = "cash", "Cash"
    BANK = "bank_transfer", "Bank transfer"
    CARD = "card", "Card"
    NONE = "none", "—"  # adjustments move no money


class Status(models.TextChoices):
    PENDING = "pending", "Pending confirmation"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    VOIDED = "voided", "Voided"


class Payment(models.Model):
    """Append-only financial ledger entry for one membership.

    * payment    – money received (amount > 0)
    * refund     – money returned (amount > 0), always linked to the payment it reverses
    * adjustment – staff-approved change to what is owed (amount may be negative = discount/waiver)
    Only ``status`` may change after creation (pending -> completed/failed, completed/pending -> voided), and only
    through ``payments.services``, which writes a PaymentAudit row for every change. Rows are never deleted.
    Only completed entries count towards balances; no card numbers or other card data are ever stored.
    """

    reference = models.CharField(max_length=30, unique=True, editable=False, default=new_reference)
    receipt_number = models.CharField(max_length=30, unique=True, editable=False)
    idempotency_key = models.CharField(max_length=64, unique=True, help_text="Makes double submissions harmless.")
    # Exactly one of membership / appointment: the thing being paid for. Appointment fees use this same ledger.
    membership = models.ForeignKey("memberships.Membership", null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    appointment = models.ForeignKey("appointments.Appointment", null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PAYMENT, db_index=True)
    refund_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField("amount (LKR)", max_digits=12, decimal_places=2)
    method = models.CharField(max_length=14, choices=Method.choices, default=Method.CASH)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED, db_index=True)
    paid_on = models.DateField(default=timezone.localdate, db_index=True)
    external_reference = models.CharField(
        "bank / terminal reference", max_length=60, blank=True,
        help_text="Bank slip or card terminal approval number. Never a card number.",
    )
    notes = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        indexes = [models.Index(fields=["membership", "status", "kind"]), models.Index(fields=["paid_on", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=(Q(membership__isnull=False, appointment__isnull=True) | Q(membership__isnull=True, appointment__isnull=False)),
                name="payment_has_exactly_one_target",
            ),
            models.CheckConstraint(
                condition=(Q(kind="adjustment") & ~Q(amount=0)) | (~Q(kind="adjustment") & Q(amount__gt=0)),
                name="payment_amount_sign",
            ),
            models.CheckConstraint(
                condition=(Q(kind="refund", refund_of__isnull=False) | (~Q(kind="refund") & Q(refund_of__isnull=True))),
                name="payment_refund_links_original",
            ),
        ]

    def __str__(self):
        return f"{self.reference} {self.get_kind_display()} {self.amount}"

    def get_absolute_url(self):
        return reverse("payments:detail", args=[self.pk])

    def save(self, *args, **kwargs):
        creating = self._state.adding
        if not creating and not (kwargs.get("update_fields") and set(kwargs["update_fields"]) <= {"status", "updated_at", "notes"}):
            raise ValueError("Payments are append-only: only the status can change.")
        if creating and not self.receipt_number:
            self.receipt_number = uuid.uuid4().hex[:30]  # placeholder until the primary key is known
        if creating and self.target is not None and self.member_id != self.target.member_id:
            raise ValueError("The payment's member must be the member of the item being paid for.")
        super().save(*args, **kwargs)
        if creating:
            prefix = "RF" if self.kind == Kind.REFUND else "RC"
            self.receipt_number = f"{prefix}-{self.created_at:%Y}-{self.pk:06d}"
            type(self).objects.filter(pk=self.pk).update(receipt_number=self.receipt_number)

    def delete(self, *args, **kwargs):
        raise ValueError("Financial records cannot be deleted. Void the entry instead.")

    @property
    def target(self):
        return self.membership if self.membership_id else self.appointment

    @property
    def is_appointment(self):
        return self.appointment_id is not None

    @property
    def item_label(self):
        """Short description of what was paid for (used in lists and receipts)."""
        if self.membership_id:
            return self.membership.plan.name
        a = self.appointment
        return f"Personal training – {a.trainer.full_name}, {timezone.localtime(a.start):%b %d, %H:%M}"

    @property
    def item_period(self):
        if self.membership_id:
            m = self.membership
            return f"{m.start_date:%d %b %Y} – {m.end_date:%d %b %Y}"
        a = self.appointment
        return f"{timezone.localtime(a.start):%d %b %Y %H:%M}–{timezone.localtime(a.end):%H:%M}"

    @property
    def target_url(self):
        return self.target.get_absolute_url()

    @property
    def signed_amount(self):
        """Effect on the member's net paid amount (adjustments change the charge, not the cash)."""
        if self.kind == Kind.PAYMENT:
            return self.amount
        return -self.amount if self.kind == Kind.REFUND else 0

    @property
    def counts(self):
        return self.status == Status.COMPLETED


class PaymentAudit(models.Model):
    """Immutable trail of every financial action (who, what, when, before/after)."""

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="audit")
    action = models.CharField(max_length=30)
    old_status = models.CharField(max_length=10, blank=True)
    new_status = models.CharField(max_length=10, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Audit records cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit records cannot be deleted.")

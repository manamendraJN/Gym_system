import uuid
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin

from .models import Method

MONEY_FIELD = dict(max_digits=12, decimal_places=2)
REAL_METHODS = [(v, l) for v, l in Method.choices if v != Method.NONE]


class IdempotentForm(StyledFormMixin, forms.Form):
    """Carries a one-time key so a double click / resubmission cannot create two entries."""

    idempotency_key = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["idempotency_key"].initial = uuid.uuid4().hex
        self.fields["idempotency_key"].widget.attrs.pop("class", None)

    def clean_idempotency_key(self):
        key = self.cleaned_data["idempotency_key"].strip()
        if not key.isalnum():
            raise forms.ValidationError("Invalid form token. Reload the page.")
        return key


class PaymentForm(IdempotentForm):
    amount = forms.DecimalField(min_value=Decimal("0.01"), **MONEY_FIELD, label="Amount (LKR)",
                                widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}))
    method = forms.ChoiceField(choices=REAL_METHODS)
    paid_on = forms.DateField(label="Payment date", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    external_reference = forms.CharField(
        max_length=60, required=False, label="Bank / terminal reference",
        help_text="Bank slip number or card terminal approval code. Never enter a card number.",
    )
    pending = forms.BooleanField(
        required=False, label="Awaiting confirmation",
        help_text="Tick for a bank transfer or card payment not yet verified. It will not count until confirmed.",
    )
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, outstanding=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paid_on"].initial = timezone.localdate()
        if outstanding is not None:
            self.fields["amount"].initial = outstanding
            self.fields["amount"].help_text = f"Balance still payable: LKR {outstanding:,.2f}"

    def clean_paid_on(self):
        day, today = self.cleaned_data["paid_on"], timezone.localdate()
        if day > today:
            raise forms.ValidationError("The payment date cannot be in the future.")
        if day < today - timedelta(days=settings.PAYMENT_MAX_BACKDATE_DAYS):
            raise forms.ValidationError(f"The payment date cannot be more than {settings.PAYMENT_MAX_BACKDATE_DAYS} days ago.")
        return day


class RefundForm(IdempotentForm):
    amount = forms.DecimalField(min_value=Decimal("0.01"), **MONEY_FIELD, label="Refund amount (LKR)",
                                widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}))
    method = forms.ChoiceField(choices=REAL_METHODS, label="Refunded via")
    external_reference = forms.CharField(max_length=60, required=False, label="Bank / terminal reference")
    reason = forms.CharField(max_length=255, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, refundable, original_method, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["amount"].initial = refundable
        self.fields["amount"].help_text = f"At most LKR {refundable:,.2f} can still be refunded."
        self.fields["method"].initial = original_method

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if len(reason) < 3:
            raise forms.ValidationError("Please give a reason.")
        return reason


class AdjustmentForm(IdempotentForm):
    amount = forms.DecimalField(
        **MONEY_FIELD, label="Adjustment (LKR)", widget=forms.NumberInput(attrs={"step": "0.01"}),
        help_text="Negative = discount or waiver (reduces what is owed); positive = extra charge.",
    )
    reason = forms.CharField(max_length=255, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if len(reason) < 3:
            raise forms.ValidationError("Please give a reason.")
        return reason

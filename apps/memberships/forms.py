from datetime import timedelta

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin

from .models import WEEKDAY_NAMES, DurationType, MembershipPlan

MAX_PRICE = 10_000_000


class PlanForm(StyledFormMixin, forms.ModelForm):
    allowed_weekdays = forms.MultipleChoiceField(
        required=False, choices=list(enumerate(WEEKDAY_NAMES)), widget=forms.CheckboxSelectMultiple,
        label="Allowed days", help_text="Leave all unticked for every day.",
    )

    class Meta:
        model = MembershipPlan
        fields = ["name", "description", "duration_type", "custom_days", "price", "benefits", "access_restrictions",
                  "allowed_weekdays", "access_from", "access_until", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "benefits": forms.Textarea(attrs={"rows": 4, "placeholder": "Gym floor access\nGroup classes\nLocker"}),
            "access_restrictions": forms.Textarea(attrs={"rows": 2}),
            "access_from": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "access_until": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, can_set_price, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_set_price = can_set_price
        self.fields["allowed_weekdays"].widget.attrs["class"] = "form-check-input"
        if self.instance.pk:
            self.initial["allowed_weekdays"] = [str(d) for d in self.instance.weekday_numbers]
        self.fields["is_active"].label = "Plan is available for new memberships"
        if not can_set_price:
            # disabled fields ignore posted values and keep the stored price (server-side)
            self.fields["price"].disabled = True
            self.fields["price"].help_text = "You do not have permission to change prices."

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if MembershipPlan.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A plan with this name already exists.")
        return name

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price < 0 or price > MAX_PRICE:
            raise forms.ValidationError(f"Enter a price between 0 and {MAX_PRICE:,}.")
        return price

    def clean_allowed_weekdays(self):
        days = sorted({int(d) for d in self.cleaned_data["allowed_weekdays"]})
        return "" if len(days) == 7 else ",".join(str(d) for d in days)

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("access_from"), cleaned.get("access_until")
        if bool(start) != bool(end):
            self.add_error("access_until" if start else "access_from", "Set both an opening and a closing time, or neither.")
        elif start and end and start >= end:
            self.add_error("access_until", "The closing time must be after the opening time.")
        dtype, days = cleaned.get("duration_type"), cleaned.get("custom_days")
        if dtype == DurationType.CUSTOM:
            if not days:
                self.add_error("custom_days", "Enter the number of days for a custom plan.")
            elif days > 3650:
                self.add_error("custom_days", "A custom plan can last at most 3650 days.")
        elif dtype:
            cleaned["custom_days"] = None
        return cleaned


class AssignForm(StyledFormMixin, forms.Form):
    plan = forms.ModelChoiceField(queryset=MembershipPlan.objects.none(), empty_label="Select a plan")
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = MembershipPlan.objects.filter(is_active=True)
        self.fields["plan"].label_from_instance = lambda p: f"{p.name} · {p.duration_label} · LKR {p.price:,.2f}"
        self.fields["start_date"].initial = timezone.localdate()

    def clean_start_date(self):
        start, today = self.cleaned_data["start_date"], timezone.localdate()
        if start < today:
            raise forms.ValidationError("The start date cannot be in the past.")
        if start > today + timedelta(days=settings.MEMBERSHIP_MAX_ADVANCE_START_DAYS):
            raise forms.ValidationError(f"The start date can be at most {settings.MEMBERSHIP_MAX_ADVANCE_START_DAYS} days ahead.")
        return start


class PlanChoiceForm(StyledFormMixin, forms.Form):
    """Used by renew (plan optional, defaults to the current one) and change-plan."""

    plan = forms.ModelChoiceField(queryset=MembershipPlan.objects.none(), empty_label=None)
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, current_plan=None, exclude_current=False, **kwargs):
        super().__init__(*args, **kwargs)
        qs = MembershipPlan.objects.filter(is_active=True)
        if exclude_current and current_plan:
            qs = qs.exclude(pk=current_plan.pk)
        elif current_plan and not current_plan.is_active:
            pass  # the renewal must pick an active plan
        self.fields["plan"].queryset = qs
        self.fields["plan"].label_from_instance = lambda p: f"{p.name} · {p.duration_label} · LKR {p.price:,.2f}"
        if current_plan and not exclude_current and qs.filter(pk=current_plan.pk).exists():
            self.fields["plan"].initial = current_plan.pk


class ReasonForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=255, widget=forms.Textarea(attrs={"rows": 3}))

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if len(reason) < 3:
            raise forms.ValidationError("Please give a reason.")
        return reason

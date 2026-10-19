from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin
from apps.accounts.models import Role
from apps.core.validators import validate_document_upload, validate_image_upload
from apps.members.forms import _clean_phone
from apps.members.models import Member

from .models import (
    EmploymentStatus, Trainer, TrainerAvailability, TrainerCertificate, TrainerCompensation, TrainerTimeOff,
)

User = get_user_model()
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")


class TrainerForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Trainer
        fields = ["full_name", "email", "phone", "photo", "bio", "qualifications", "specialties", "employment_status",
                  "hire_date", "hourly_rate", "user"]
        widgets = {
            "hire_date": DATE, "bio": forms.Textarea(attrs={"rows": 3}), "qualifications": forms.Textarea(attrs={"rows": 3}),
            "hourly_rate": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "photo": forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
        }
        labels = {"user": "Login account", "employment_status": "Employment status"}
        help_texts = {"photo": "JPEG, PNG or WebP, up to 2 MB."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only trainer-role logins that are not already attached to another trainer profile.
        free = User.objects.filter(role=Role.TRAINER, is_active=True)
        taken = Trainer.objects.exclude(pk=self.instance.pk).exclude(user=None).values("user")
        self.fields["user"].queryset = free.exclude(pk__in=taken)
        self.fields["user"].required = False
        self.fields["user"].help_text = "Link the trainer's login (create it under Users with the role Trainer) so they can sign in."

    def clean_full_name(self):
        name = " ".join(self.cleaned_data["full_name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Enter the trainer's full name.")
        return name

    def clean_phone(self):
        return _clean_phone(self.cleaned_data["phone"])

    def clean_photo(self):
        return validate_image_upload(self.cleaned_data.get("photo"))

    def clean_hourly_rate(self):
        rate = self.cleaned_data["hourly_rate"]
        if rate < 0 or rate > Decimal("1000000"):
            raise forms.ValidationError("Enter a rate between 0 and 1,000,000.")
        return rate


class AssignForm(StyledFormMixin, forms.Form):
    trainer = forms.ModelChoiceField(queryset=Trainer.objects.none())
    member = forms.CharField(label="Member ID or phone number", max_length=30)
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trainer"].queryset = Trainer.objects.exclude(employment_status=EmploymentStatus.TERMINATED)

    def clean_member(self):
        from apps.members.models import normalize_phone

        value = self.cleaned_data["member"].strip()
        member = Member.objects.filter(member_id__iexact=value).first()
        if member is None and normalize_phone(value).lstrip("+"):
            member = Member.objects.filter(phone=normalize_phone(value)).first()
        if member is None:
            raise forms.ValidationError("No member found with that ID or phone number.")
        return member


class AvailabilityForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainerAvailability
        fields = ["weekday", "start_time", "end_time"]
        widgets = {"start_time": TIME, "end_time": TIME}

    def __init__(self, *args, trainer, **kwargs):
        self.trainer = trainer
        super().__init__(*args, **kwargs)

    def clean(self):
        c = super().clean()
        s, e, d = c.get("start_time"), c.get("end_time"), c.get("weekday")
        if s and e:
            if e <= s:
                self.add_error("end_time", "The end time must be after the start time.")
            elif d is not None and self.trainer.availability.filter(weekday=d, start_time__lt=e, end_time__gt=s).exists():
                raise forms.ValidationError("This window overlaps one that already exists for that day.")
        return c


class TimeOffForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainerTimeOff
        fields = ["start_date", "end_date", "reason"]
        widgets = {"start_date": DATE, "end_date": DATE}

    def clean(self):
        c = super().clean()
        if c.get("start_date") and c.get("end_date") and c["end_date"] < c["start_date"]:
            self.add_error("end_date", "The end date cannot be before the start date.")
        return c


class CertificateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainerCertificate
        fields = ["title", "issuer", "issued_on", "expires_on", "file"]
        widgets = {"issued_on": DATE, "expires_on": DATE, "file": forms.FileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png"})}
        help_texts = {"file": "PDF, JPEG or PNG, up to 5 MB. Stored privately."}

    def clean_file(self):
        return validate_document_upload(self.cleaned_data["file"])

    def clean(self):
        c = super().clean()
        if c.get("issued_on") and c.get("expires_on") and c["expires_on"] < c["issued_on"]:
            self.add_error("expires_on", "The expiry date cannot be before the issue date.")
        return c


class CompensationForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TrainerCompensation
        fields = ["kind", "amount", "period_start", "period_end", "notes"]
        widgets = {"period_start": DATE, "period_end": DATE, "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"})}

    def clean(self):
        c = super().clean()
        if c.get("period_start") and c.get("period_end") and c["period_end"] < c["period_start"]:
            self.add_error("period_end", "The period end cannot be before its start.")
        return c

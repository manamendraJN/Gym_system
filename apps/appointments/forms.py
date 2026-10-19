from datetime import datetime

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin
from apps.members.models import Member, normalize_phone
from apps.trainers.models import Trainer


def combine_local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class BookingForm(StyledFormMixin, forms.Form):
    """Used by staff (with a member ID) and by the portal (member and trainer fixed by the view)."""

    member = forms.CharField(label="Member ID or phone number", max_length=30, required=False)
    trainer = forms.ModelChoiceField(queryset=Trainer.objects.none(), empty_label="Select a trainer")
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time", "step": 60 * settings.APPOINTMENT_SLOT_STEP_MINUTES}, format="%H:%M"),
                           help_text="Pick a free slot below, or type a time on the quarter hour.")
    duration = forms.TypedChoiceField(coerce=int, choices=[(d, f"{d} minutes") for d in settings.APPOINTMENT_DURATIONS])
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, trainers, staff=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trainer"].queryset = trainers
        if staff:
            self.fields["member"].required = True
        else:
            del self.fields["member"]
        self.fields["duration"].initial = 60

    def clean_member(self):
        value = self.cleaned_data["member"].strip()
        member = Member.objects.filter(member_id__iexact=value).first()
        if member is None and normalize_phone(value).lstrip("+"):
            member = Member.objects.filter(phone=normalize_phone(value)).first()
        if member is None:
            raise forms.ValidationError("No member found with that ID or phone number.")
        return member

    def start(self):
        return combine_local(self.cleaned_data["date"], self.cleaned_data["time"])


class RescheduleForm(StyledFormMixin, forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time", "step": 60 * settings.APPOINTMENT_SLOT_STEP_MINUTES}, format="%H:%M"))

    def start(self):
        return combine_local(self.cleaned_data["date"], self.cleaned_data["time"])

from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin

from .models import Member, normalize_phone

MAX_PHOTO_BYTES = 2 * 1024 * 1024
ALLOWED_PHOTO_FORMATS = {"JPEG", "PNG", "WEBP"}


def _clean_phone(value):
    phone = normalize_phone(value)
    digits = phone.lstrip("+")
    if not 9 <= len(digits) <= 15:
        raise forms.ValidationError("Enter a valid phone number (9–15 digits).")
    return phone


class MemberForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Member
        fields = [
            "full_name", "phone", "email", "date_of_birth", "address", "emergency_contact_name",
            "emergency_contact_phone", "registration_date", "photo", "fitness_goals",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "registration_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "address": forms.Textarea(attrs={"rows": 2}),
            "fitness_goals": forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. lose weight, build strength (optional)"}),
            "photo": forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
        }
        labels = {"emergency_contact_name": "Emergency contact name", "emergency_contact_phone": "Emergency contact phone"}
        help_texts = {"photo": "JPEG, PNG or WebP, up to 2 MB."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._old_photo = self.instance.photo.name if self.instance.pk and self.instance.photo else ""

    def clean_full_name(self):
        name = " ".join(self.cleaned_data["full_name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Enter the member's full name.")
        return name

    def clean_phone(self):
        phone = _clean_phone(self.cleaned_data["phone"])
        # Compare on the last 9 digits so 0771234567 and +94771234567 count as the same number.
        tail = phone.lstrip("+")[-9:]
        clash = Member.objects.filter(phone__endswith=tail).exclude(pk=self.instance.pk).first()
        if clash:
            raise forms.ValidationError(f"This phone number is already registered to {clash.full_name} ({clash.member_id}).")
        return phone

    def clean_emergency_contact_phone(self):
        return _clean_phone(self.cleaned_data["emergency_contact_phone"])

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if email:
            clash = Member.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).first()
            if clash:
                raise forms.ValidationError(f"This email is already registered to {clash.full_name} ({clash.member_id}).")
        return email

    def clean_date_of_birth(self):
        dob = self.cleaned_data["date_of_birth"]
        today = timezone.localdate()
        if dob >= today:
            raise forms.ValidationError("Date of birth must be in the past.")
        if dob < today - timedelta(days=365 * 100):
            raise forms.ValidationError("Please check the date of birth.")
        return dob

    def clean_registration_date(self):
        reg = self.cleaned_data["registration_date"]
        if reg > timezone.localdate():
            raise forms.ValidationError("Registration date cannot be in the future.")
        return reg

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and hasattr(photo, "image"):  # a newly uploaded file (already verified by Pillow)
            if photo.size > MAX_PHOTO_BYTES:
                raise forms.ValidationError("The photo must be 2 MB or smaller.")
            if photo.image.format not in ALLOWED_PHOTO_FORMATS:
                raise forms.ValidationError("Use a JPEG, PNG or WebP image.")
        return photo

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("phone") and cleaned.get("phone") == cleaned.get("emergency_contact_phone"):
            self.add_error("emergency_contact_phone", "The emergency contact should be a different number from the member's.")
        return cleaned

    def save(self, commit=True):
        member = super().save(commit)
        if commit and self._old_photo and self._old_photo != (member.photo.name if member.photo else ""):
            member.photo.storage.delete(self._old_photo)
        return member

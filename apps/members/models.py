import secrets
import uuid
from datetime import date

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import phone_validator


def member_photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"members/photos/{uuid.uuid4().hex}.{ext}"


def normalize_phone(value):
    """Keep digits and a single leading '+' (so "077 123-4567" and "0771234567" compare equal)."""
    value = (value or "").strip()
    digits = "".join(ch for ch in value if ch.isdigit())
    return ("+" if value.startswith("+") else "") + digits


def phone_search_term(query):
    """The digits to look for in phone numbers, or "" when the query is not phone-like (so searching
    "APPR-1" or a name never matches phone numbers by accident)."""
    query = (query or "").strip()
    if not query or any(not (ch.isdigit() or ch in " +-()") for ch in query):
        return ""
    digits = normalize_phone(query)
    return digits if len(digits.lstrip("+")) >= 3 else ""


def new_qr_token():
    """Opaque, non-guessable (256-bit) check-in credential. Carries no personal data."""
    return secrets.token_urlsafe(32)


class Member(models.Model):
    member_id = models.CharField(max_length=20, unique=True, editable=False)
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, unique=True, validators=[phone_validator])
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    date_of_birth = models.DateField()
    emergency_contact_name = models.CharField(max_length=150)
    emergency_contact_phone = models.CharField(max_length=20, validators=[phone_validator])
    registration_date = models.DateField(default=timezone.localdate)
    photo = models.ImageField(upload_to=member_photo_path, blank=True)
    fitness_goals = models.TextField(blank=True)
    # Secret behind the member's check-in QR code. Treat like a password: never log it or put it in a URL.
    qr_token = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    # Optional login for the member portal (role "member"). Everything a member sees is filtered on this link.
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="member_profile")
    # Deactivation hides the member from day-to-day work but never deletes any history.
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name"]
        constraints = [
            # Case-insensitive uniqueness for emails that were actually provided.
            models.UniqueConstraint(
                Lower("email"), condition=~models.Q(email=""), name="unique_member_email_ci"
            ),
        ]

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        self.emergency_contact_phone = normalize_phone(self.emergency_contact_phone)
        self.email = self.email.strip().lower()
        creating = self._state.adding
        if not self.qr_token:
            self.qr_token = new_qr_token()
        if creating and not self.member_id:
            self.member_id = uuid.uuid4().hex[:20]  # placeholder until the primary key is known
        super().save(*args, **kwargs)
        if creating:
            self.member_id = f"{settings.MEMBER_ID_PREFIX}{self.pk:06d}"
            type(self).objects.filter(pk=self.pk).update(member_id=self.member_id)

    def get_absolute_url(self):
        return reverse("members:detail", args=[self.pk])

    @property
    def age(self):
        today = timezone.localdate()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    @property
    def initials(self):
        return (self.full_name.strip()[:1] or "?").upper()

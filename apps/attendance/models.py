from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Method(models.TextChoices):
    MANUAL = "manual", "Front desk"
    QR = "qr", "QR code"


class Attendance(models.Model):
    """One gym visit. Visits are never deleted; a missed check-out is closed automatically and flagged."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="attendance")
    membership = models.ForeignKey(
        "memberships.Membership", null=True, blank=True, on_delete=models.PROTECT, related_name="attendance",
        help_text="The membership that granted access.",
    )
    check_in = models.DateTimeField(default=timezone.now, db_index=True)
    check_out = models.DateTimeField(null=True, blank=True)
    auto_closed = models.BooleanField(default=False, help_text="Check-out was not recorded; closed automatically.")
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.MANUAL)
    checked_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    checked_out_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-check_in", "-id"]
        constraints = [
            # Duplicate check-in prevention enforced by the database: at most one open visit per member.
            models.UniqueConstraint(fields=["member"], condition=Q(check_out__isnull=True), name="one_open_visit_per_member"),
            models.CheckConstraint(condition=Q(check_out__isnull=True) | Q(check_out__gte=F("check_in")), name="attendance_out_after_in"),
        ]

    def __str__(self):
        return f"{self.member} @ {self.check_in:%Y-%m-%d %H:%M}"

    @property
    def is_open(self):
        return self.check_out is None

    @property
    def duration(self):
        end = self.check_out or timezone.now()
        return end - self.check_in

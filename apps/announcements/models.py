from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone


class Audience(models.TextChoices):
    EVERYONE = "everyone", "Everyone (members and staff)"
    MEMBERS_ALL = "members_all", "All members"
    MEMBERS_ACTIVE = "members_active", "Members with an active membership"
    MEMBERS_EXPIRED = "members_expired", "Members whose membership has expired"
    STAFF = "staff", "All staff"
    TRAINERS = "trainers", "Trainers"


class AnnouncementKind(models.TextChoices):
    INFO = "info", "General information"
    CLOSURE = "closure", "Gym closure notice"
    OPERATIONAL = "operational", "Operational update"
    PROMO = "promo", "Offer / promotion"


class Announcement(models.Model):
    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000)
    kind = models.CharField(max_length=12, choices=AnnouncementKind.choices, default=AnnouncementKind.INFO)
    audience = models.CharField(max_length=16, choices=Audience.choices, default=Audience.MEMBERS_ALL)
    publish_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    send_notification = models.BooleanField("Also notify the audience", default=True,
                                            help_text="Creates a notification (and e-mail/SMS/WhatsApp copies where configured) when it is published.")
    notified_at = models.DateTimeField(null=True, blank=True, editable=False)
    notified_count = models.PositiveIntegerField(default=0, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-publish_at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(expires_at__isnull=True) | Q(expires_at__gt=F("publish_at")), name="announcement_expiry_after_publish")]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("announcements:list")

    def status_at(self, now=None):
        now = now or timezone.now()
        if not self.is_active:
            return "inactive"
        if self.publish_at > now:
            return "scheduled"
        if self.expires_at and self.expires_at <= now:
            return "expired"
        return "live"

    @property
    def status_key(self):
        return self.status_at()

    @property
    def status_label(self):
        return {"inactive": "Inactive", "scheduled": "Scheduled", "expired": "Expired", "live": "Live"}[self.status_key]

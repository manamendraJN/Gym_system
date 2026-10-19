from django.conf import settings
from django.db import IntegrityError, models, transaction


class ActivityLog(models.Model):
    """Audit trail shown in the dashboard's "Recent activities" panel."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="activities"
    )
    action = models.CharField(max_length=50, db_index=True)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.user} – {self.description}"

    @classmethod
    def record(cls, user, action, description):
        return cls.objects.create(user=user, action=action, description=description[:255])

    @property
    def icon(self):
        for prefix, icon in (("user_", "person-gear"), ("member_", "person-badge"), ("plan_", "tags"),
                             ("membership_", "card-checklist")):
            if self.action.startswith(prefix):
                return icon
        return {"login": "box-arrow-in-right", "logout": "box-arrow-right"}.get(self.action, "activity")


class GymSettings(models.Model):
    """Single-row gym profile (see apps/core/gym.py)."""

    name = models.CharField("gym name", max_length=100)
    tagline = models.CharField(max_length=150, blank=True)
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    receipt_footer = models.CharField(max_length=255, blank=True, help_text="Printed at the bottom of receipts, e.g. a thank-you or refund policy line.")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "gym settings"

    def __str__(self):
        return self.name


class Notification(models.Model):
    """In-app message for one user (appointment booked / changed / reminder ...)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=120)
    message = models.CharField(max_length=255)
    url = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=30, default="general", db_index=True)
    # Reminders carry a key such as "membership-expiring:42:7"; the same key can only exist once per user, so a
    # scheduled job that runs twice (or a retry) can never send the same reminder again.
    dedupe_key = models.CharField(max_length=120, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"])]
        constraints = [
            models.UniqueConstraint(fields=["user", "dedupe_key"], condition=models.Q(dedupe_key__isnull=False), name="unique_notification_dedupe_key"),
        ]

    def __str__(self):
        return f"{self.user}: {self.title}"

    @classmethod
    def send(cls, user, title, message, url="", kind="general", dedupe_key=None, external=False):
        """Create an in-app notification. Returns None (and creates nothing) for inactive users or when a notification
        with the same dedupe_key already exists for the user. `external=True` additionally queues e-mail / SMS /
        WhatsApp deliveries for the channels that are configured and that the user allows."""
        if user is None or not user.is_active:
            return None
        if dedupe_key and cls.objects.filter(user=user, dedupe_key=dedupe_key).exists():
            return None
        try:
            with transaction.atomic():
                note = cls.objects.create(user=user, title=title[:120], message=message[:255], url=url[:200], kind=kind, dedupe_key=dedupe_key)
        except IntegrityError:  # a concurrent run created it first
            return None
        if external:
            from apps.notifications.delivery import queue_deliveries

            queue_deliveries(note)
        return note


class SampleRecord(models.Model):
    """Ownership register for sample data: one row per record created by `load_sample_data`.
    `cleanup_sample_data` deletes only records listed here, so real data is never touched."""

    model_label = models.CharField(max_length=100)
    object_pk = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["model_label", "object_pk"], name="unique_sample_record")]
        indexes = [models.Index(fields=["model_label"])]

    def __str__(self):
        return f"{self.model_label}#{self.object_pk}"

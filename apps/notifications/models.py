from django.conf import settings
from django.db import models


def parse_days(text, allow_negative=False):
    """"14, 7,1" -> [14, 7, 1] (sorted high to low, de-duplicated). Raises ValueError on bad input."""
    values = set()
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        number = int(part)
        if number < 0 and not allow_negative:
            raise ValueError("Negative values are not allowed here.")
        if abs(number) > 365:
            raise ValueError("Values must be within 365 days.")
        values.add(number)
    if not values:
        raise ValueError("Enter at least one value.")
    return sorted(values, reverse=True)


class NotificationSettings(models.Model):
    """One row of business-configurable reminder periods and channel switches (edit under Notification settings)."""

    membership_expiry_days = models.CharField("Remind before membership ends (days)", max_length=60, default="14,7,1",
                                              help_text="Comma-separated, e.g. 14,7,1. One reminder is sent per value.")
    payment_due_grace_days = models.PositiveSmallIntegerField("Payment due this many days after membership start", default=7)
    payment_due_offsets = models.CharField("Payment reminders (days before due; 0 = due date; negative = overdue)", max_length=60, default="3,0,-3,-7")
    appointment_reminder_hours = models.PositiveSmallIntegerField("Remind about training sessions this many hours before", default=24)
    maintenance_reminder_days = models.CharField("Remind before equipment maintenance is due (days)", max_length=60, default="7,1,0")
    email_enabled = models.BooleanField("Send e-mail copies", default=True)
    sms_enabled = models.BooleanField("Send SMS copies", default=False)
    whatsapp_enabled = models.BooleanField("Send WhatsApp copies", default=False)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "notification settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def expiry_days(self):
        return parse_days(self.membership_expiry_days)

    def due_offsets(self):
        return parse_days(self.payment_due_offsets, allow_negative=True)

    def maintenance_days(self):
        return parse_days(self.maintenance_reminder_days)


class NotificationPreference(models.Model):
    """Per-user opt-in for external copies. In-app notifications are always on. SMS / WhatsApp are opt-in."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference")
    email_enabled = models.BooleanField("E-mail", default=True)
    sms_enabled = models.BooleanField("SMS", default=False)
    whatsapp_enabled = models.BooleanField("WhatsApp", default=False)


class Channel(models.TextChoices):
    EMAIL = "email", "E-mail"
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"


class DeliveryStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENT = "sent", "Handed to provider (not confirmed delivered)"
    DELIVERED = "delivered", "Delivered (confirmed by provider)"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Not sent"


class NotificationDelivery(models.Model):
    """Honest record of an external copy of a notification. `sent` only means the provider accepted the message;
    `delivered` is set solely from a provider confirmation (e.g. the Twilio status callback)."""

    notification = models.ForeignKey("core.Notification", on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=10, choices=Channel.choices)
    address = models.CharField(max_length=120)
    status = models.CharField(max_length=10, choices=DeliveryStatus.choices, default=DeliveryStatus.QUEUED, db_index=True)
    provider_message_id = models.CharField(max_length=80, blank=True, db_index=True)
    detail = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["notification", "channel"], name="one_delivery_per_channel")]

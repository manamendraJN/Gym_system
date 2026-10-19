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


def branding_path(instance, filename):
    import uuid

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    return f"branding/{uuid.uuid4().hex}.{ext}"


class Appearance(models.Model):
    """Single-row (pk=1) system customisation: branding images, login page text, theme colours and layout preferences.
    Read through apps/core/appearance.py, which validates/normalises everything before it reaches a template."""

    class Mode(models.TextChoices):
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"
        SYSTEM = "system", "Follow the device setting"

    class SidebarStyle(models.TextChoices):
        DARK = "dark", "Dark"
        LIGHT = "light", "Light"

    class SidebarWidth(models.TextChoices):
        EXPANDED = "expanded", "Expanded"
        COMPACT = "compact", "Compact"

    class Density(models.TextChoices):
        COMFORTABLE = "comfortable", "Comfortable"
        COMPACT = "compact", "Compact"

    class FontScale(models.TextChoices):
        SMALL = "small", "Small"
        DEFAULT = "default", "Default"
        LARGE = "large", "Large"

    class FontFamily(models.TextChoices):
        INTER = "inter", "Inter"
        SYSTEM = "system", "System font"

    class StatColumns(models.TextChoices):
        FOUR = "4", "Four per row"
        THREE = "3", "Three per row"

    # branding
    logo = models.ImageField(upload_to=branding_path, blank=True)
    favicon = models.ImageField(upload_to=branding_path, blank=True)
    login_background = models.ImageField(upload_to=branding_path, blank=True)
    login_title = models.CharField(max_length=80, blank=True, help_text="Heading on the sign-in page. Leave empty for the default.")
    login_message = models.CharField(max_length=200, blank=True, help_text="Short welcome message under the heading.")
    # theme
    theme_preset = models.CharField(max_length=20, default="graphite")
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.LIGHT)
    primary_color = models.CharField(max_length=7, blank=True)
    secondary_color = models.CharField(max_length=7, blank=True)
    accent_color = models.CharField(max_length=7, blank=True)
    background_color = models.CharField(max_length=7, blank=True)
    surface_color = models.CharField(max_length=7, blank=True)
    text_color = models.CharField(max_length=7, blank=True)
    border_color = models.CharField(max_length=7, blank=True)
    # layout
    sidebar_style = models.CharField(max_length=10, choices=SidebarStyle.choices, default=SidebarStyle.DARK)
    sidebar_width = models.CharField(max_length=10, choices=SidebarWidth.choices, default=SidebarWidth.EXPANDED)
    density = models.CharField(max_length=12, choices=Density.choices, default=Density.COMFORTABLE)
    font_scale = models.CharField(max_length=10, choices=FontScale.choices, default=FontScale.DEFAULT)
    font_family = models.CharField(max_length=10, choices=FontFamily.choices, default=FontFamily.INTER)
    stat_columns = models.CharField(max_length=1, choices=StatColumns.choices, default=StatColumns.FOUR)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "appearance"

    def __str__(self):
        return "System appearance"

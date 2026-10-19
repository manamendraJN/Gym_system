import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone


class EquipmentCategory(models.TextChoices):
    CARDIO = "cardio", "Cardio machine"
    STRENGTH = "strength", "Strength machine"
    FREE_WEIGHTS = "free_weights", "Free weights"
    FUNCTIONAL = "functional", "Functional / studio"
    ACCESSORIES = "accessories", "Accessories"
    FACILITY = "facility", "Facility / building"
    OTHER = "other", "Other"


class EquipmentStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    IN_USE = "in_use", "In use"
    MAINTENANCE = "maintenance", "Under maintenance"
    OUT_OF_SERVICE = "out_of_service", "Out of service"


class Condition(models.TextChoices):
    EXCELLENT = "excellent", "Excellent"
    GOOD = "good", "Good"
    FAIR = "fair", "Fair"
    POOR = "poor", "Poor"


class Equipment(models.Model):
    code = models.CharField("equipment code", max_length=30, unique=True)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=14, choices=EquipmentCategory.choices, default=EquipmentCategory.OTHER, db_index=True)
    serial_number = models.CharField(max_length=80, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField("purchase cost (LKR)", max_digits=12, decimal_places=2, null=True, blank=True)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="equipment")
    warranty_expires = models.DateField(null=True, blank=True)
    warranty_notes = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=100, blank=True)
    condition = models.CharField(max_length=10, choices=Condition.choices, default=Condition.GOOD)
    status = models.CharField(max_length=14, choices=EquipmentStatus.choices, default=EquipmentStatus.AVAILABLE, db_index=True)
    maintenance_interval_days = models.PositiveIntegerField(null=True, blank=True, help_text="Used to schedule the next service automatically.")
    last_maintenance_date = models.DateField(null=True, blank=True)
    next_maintenance_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "code"]
        verbose_name_plural = "equipment"
        constraints = [
            models.CheckConstraint(condition=Q(purchase_cost__isnull=True) | Q(purchase_cost__gte=0), name="equipment_cost_non_negative"),
        ]

    def __str__(self):
        return f"{self.code} – {self.name}"

    def save(self, *args, **kwargs):
        """A blank code is replaced by EQ-0001, EQ-0002 ... (from the primary key) right after the first save."""
        self.code = self.code.strip().upper()
        generate = not self.code
        if generate:
            self.code = f"TMP-{uuid.uuid4().hex[:20]}"
        super().save(*args, **kwargs)
        if generate:
            self.code = f"EQ-{self.pk:04d}"
            type(self).objects.filter(pk=self.pk).update(code=self.code)

    def get_absolute_url(self):
        return reverse("equipment:detail", args=[self.pk])

    @property
    def under_warranty(self):
        return bool(self.warranty_expires and self.warranty_expires >= timezone.localdate())

    @property
    def maintenance_state(self):
        """overdue / soon / ok / none"""
        d = self.next_maintenance_date
        if not d:
            return "none"
        today = timezone.localdate()
        if d < today:
            return "overdue"
        return "soon" if (d - today).days <= 14 else "ok"

    @property
    def is_usable(self):
        return self.status in (EquipmentStatus.AVAILABLE, EquipmentStatus.IN_USE)


class FaultSeverity(models.TextChoices):
    MINOR = "minor", "Minor – still usable"
    MAJOR = "major", "Major – unsafe / unusable"


class FaultReport(models.Model):
    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="faults")
    description = models.TextField()
    severity = models.CharField(max_length=6, choices=FaultSeverity.choices, default=FaultSeverity.MINOR)
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reported_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-reported_at", "-id"]

    @property
    def is_open(self):
        return self.resolved_at is None


class MaintenanceKind(models.TextChoices):
    ROUTINE = "routine", "Routine service"
    REPAIR = "repair", "Repair"
    INSPECTION = "inspection", "Inspection"


class MaintenanceRecord(models.Model):
    """Append-only maintenance / repair history. A cost is also recorded once as a linked Expense (see expenses)."""

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="maintenance")
    kind = models.CharField(max_length=10, choices=MaintenanceKind.choices, default=MaintenanceKind.ROUTINE)
    performed_on = models.DateField(default=timezone.localdate, db_index=True)
    description = models.CharField(max_length=255)
    cost = models.DecimalField("cost (LKR)", max_digits=12, decimal_places=2, default=0)
    technician = models.CharField("technician / company", max_length=150, blank=True)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="maintenance")
    fault = models.ForeignKey(FaultReport, null=True, blank=True, on_delete=models.PROTECT, related_name="repairs")
    next_due = models.DateField(null=True, blank=True)
    condition_after = models.CharField(max_length=10, choices=Condition.choices, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-performed_on", "-id"]
        constraints = [models.CheckConstraint(condition=Q(cost__gte=0), name="maintenance_cost_non_negative")]

    def delete(self, *args, **kwargs):
        raise ValueError("Maintenance history cannot be deleted.")

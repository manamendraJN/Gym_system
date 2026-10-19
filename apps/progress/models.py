from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone


class MeasurementType(models.Model):
    """Configurable body measurement (waist, chest, body fat % ...). Staff can add more."""

    name = models.CharField(max_length=60)
    unit = models.CharField(max_length=10, default="cm")
    is_active = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="unique_measurement_type_ci")]

    def __str__(self):
        return f"{self.name} ({self.unit})"


class ProgressRecord(models.Model):
    """One dated check-in of body weight, height and measurements. Records are not edited after the fact."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="progress_records")
    recorded_on = models.DateField(default=timezone.localdate, db_index=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    height_cm = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    source = models.CharField(max_length=10, default="staff")  # member | staff
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_on", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(weight_kg__isnull=True) | (Q(weight_kg__gte=20) & Q(weight_kg__lte=400)), name="progress_weight_range"),
            models.CheckConstraint(condition=Q(height_cm__isnull=True) | (Q(height_cm__gte=100) & Q(height_cm__lte=250)), name="progress_height_range"),
        ]

    @property
    def bmi(self):
        """Indicative only (weight / height²). Never labelled or interpreted as a diagnosis."""
        if self.weight_kg and self.height_cm:
            metres = float(self.height_cm) / 100
            return round(float(self.weight_kg) / (metres * metres), 1)
        return None


class ProgressMeasurement(models.Model):
    record = models.ForeignKey(ProgressRecord, on_delete=models.CASCADE, related_name="measurements")
    type = models.ForeignKey(MeasurementType, on_delete=models.PROTECT, related_name="values")
    value = models.DecimalField(max_digits=6, decimal_places=1)

    class Meta:
        ordering = ["type__position", "type__name"]
        constraints = [
            models.UniqueConstraint(fields=["record", "type"], name="one_value_per_type_per_record"),
            models.CheckConstraint(condition=Q(value__gt=0) & Q(value__lte=1000), name="measurement_value_range"),
        ]

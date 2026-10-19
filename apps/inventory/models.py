import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.urls import reverse


class ItemCategory(models.TextChoices):
    SUPPLEMENTS = "supplements", "Supplements & drinks"
    CLEANING = "cleaning", "Cleaning supplies"
    TOWELS = "towels", "Towels & linen"
    ACCESSORIES = "accessories", "Gym accessories"
    OFFICE = "office", "Office & stationery"
    OTHER = "other", "Other"


class InventoryItem(models.Model):
    sku = models.CharField("SKU / item code", max_length=40, unique=True)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=12, choices=ItemCategory.choices, default=ItemCategory.OTHER, db_index=True)
    unit = models.CharField(max_length=20, default="pcs", help_text="e.g. pcs, bottle, kg, box")
    # Only changed through inventory.services.move_stock, which records a StockMovement for every change.
    quantity = models.IntegerField(default=0)
    min_stock = models.PositiveIntegerField("minimum stock level", default=0)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="items")
    last_unit_cost = models.DecimalField("last purchase price per unit (LKR)", max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gte=0), name="inventory_quantity_non_negative")]

    def __str__(self):
        return f"{self.sku} – {self.name}"

    def save(self, *args, **kwargs):
        self.sku = self.sku.strip().upper()
        generate = not self.sku
        if generate:
            self.sku = f"TMP-{uuid.uuid4().hex[:30]}"
        super().save(*args, **kwargs)
        if generate:
            self.sku = f"SKU-{self.pk:05d}"
            type(self).objects.filter(pk=self.pk).update(sku=self.sku)

    def get_absolute_url(self):
        return reverse("inventory:detail", args=[self.pk])

    @property
    def is_low(self):
        return self.is_active and self.quantity <= self.min_stock

    @property
    def stock_state(self):
        if self.quantity <= 0:
            return "out"
        return "low" if self.quantity <= self.min_stock else "ok"


class MovementKind(models.TextChoices):
    INITIAL = "initial", "Opening stock"
    PURCHASE = "purchase", "Stock purchase"
    USAGE = "usage", "Used / sold"
    ADJUSTMENT = "adjustment", "Stock adjustment"


class StockMovement(models.Model):
    """Append-only ledger of every stock change. quantity_after is a snapshot, so the history can be audited:
    each row's quantity_after equals the previous row's plus its delta."""

    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=12, choices=MovementKind.choices)
    delta = models.IntegerField(help_text="Signed change in quantity.")
    quantity_after = models.IntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reference = models.CharField("invoice / reference", max_length=60, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(delta=0), name="stockmovement_delta_non_zero"),
            models.CheckConstraint(condition=Q(quantity_after__gte=0), name="stockmovement_after_non_negative"),
            models.CheckConstraint(condition=Q(unit_cost__isnull=True) | Q(unit_cost__gte=0), name="stockmovement_cost_non_negative"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Stock movements are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Stock movements cannot be deleted.")

    @property
    def total_cost(self):
        return None if self.unit_cost is None else self.unit_cost * abs(self.delta)

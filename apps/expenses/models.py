import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.core.storage import PrivateStorage


def receipt_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"expenses/receipts/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}.{ext}"


class ExpenseCategory(models.Model):
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"
        constraints = [models.UniqueConstraint(Lower("name"), name="unique_expense_category_ci")]

    def __str__(self):
        return self.name


class Expense(models.Model):
    """An operating cost. This table is the single source of truth for expenses: maintenance and stock-purchase
    costs create exactly one linked Expense (one-to-one links below) so nothing is ever counted twice.
    Expenses are never deleted; mistakes are voided with a reason and excluded from every total."""

    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    amount = models.DecimalField("amount (LKR)", max_digits=12, decimal_places=2)
    expense_date = models.DateField(default=timezone.localdate, db_index=True)
    description = models.CharField(max_length=255)
    supplier = models.ForeignKey("suppliers.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="expenses")
    reference = models.CharField("invoice / receipt no.", max_length=60, blank=True)
    receipt = models.FileField(upload_to=receipt_path, storage=PrivateStorage(), blank=True)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Where a system-generated expense came from (at most one expense per source).
    maintenance_record = models.OneToOneField("equipment.MaintenanceRecord", null=True, blank=True, on_delete=models.PROTECT, related_name="expense")
    stock_movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="expense")
    equipment = models.OneToOneField("equipment.Equipment", null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_expense")
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-expense_date", "-id"]
        indexes = [models.Index(fields=["expense_date", "category"])]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_amount_positive"),
            models.CheckConstraint(condition=Q(voided_at__isnull=True) | ~Q(void_reason=""), name="expense_void_has_reason"),
        ]

    def __str__(self):
        return f"{self.expense_date} {self.category} {self.amount}"

    def get_absolute_url(self):
        return reverse("expenses:detail", args=[self.pk])

    def delete(self, *args, **kwargs):
        raise ValueError("Expenses cannot be deleted. Void the expense instead.")

    @property
    def is_void(self):
        return self.voided_at is not None

    @property
    def is_linked(self):
        return bool(self.maintenance_record_id or self.stock_movement_id or self.equipment_id)

    @property
    def source_label(self):
        if self.maintenance_record_id:
            return "Maintenance record"
        if self.stock_movement_id:
            return "Stock purchase"
        if self.equipment_id:
            return "Equipment purchase"
        return ""


def active_expenses():
    return Expense.objects.filter(voided_at__isnull=True)

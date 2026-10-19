from django.db import transaction
from django.utils import timezone

from apps.core.models import ActivityLog

from .models import Expense, ExpenseCategory

DEFAULT_CATEGORIES = ["Rent", "Electricity", "Water", "Salaries", "Equipment", "Maintenance", "Cleaning supplies",
                      "Inventory & supplies", "Marketing", "Other operating costs"]


class ExpenseError(Exception):
    """Safe-to-show message."""


def category(name):
    """Category by (case-insensitive) name, created on first use."""
    return ExpenseCategory.objects.filter(name__iexact=name).first() or ExpenseCategory.objects.create(name=name)


@transaction.atomic
def record_expense(category_obj, amount, expense_date, description, user, supplier=None, reference="", receipt=None, **source):
    """Create an expense. `source` may hold one of maintenance_record / stock_movement / equipment (system-generated)."""
    expense = Expense.objects.create(
        category=category_obj, amount=amount, expense_date=expense_date, description=description[:255], supplier=supplier,
        reference=reference[:60], entered_by=user, **source)
    if receipt:
        expense.receipt = receipt
        expense.save(update_fields=["receipt", "updated_at"])
    ActivityLog.record(user, "expense_recorded", f"Expense LKR {expense.amount:,.2f} – {category_obj.name}: {expense.description}")
    return expense


@transaction.atomic
def update_expense(expense, user, old_amount):
    expense.save()
    note = f" (amount LKR {old_amount:,.2f} → {expense.amount:,.2f})" if old_amount != expense.amount else ""
    ActivityLog.record(user, "expense_updated", f"Updated expense #{expense.pk}{note}")
    return expense


@transaction.atomic
def void_expense(expense, user, reason):
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.is_void:
        raise ExpenseError("This expense is already void.")
    if expense.is_linked:
        raise ExpenseError(f"This expense was generated from a {expense.source_label.lower()} and cannot be voided here.")
    if not reason.strip():
        raise ExpenseError("A reason is required.")
    expense.voided_at, expense.voided_by, expense.void_reason = timezone.now(), user, reason.strip()[:255]
    expense.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_at"])
    ActivityLog.record(user, "expense_voided", f"Voided expense #{expense.pk} (LKR {expense.amount:,.2f}): {expense.void_reason}")
    return expense

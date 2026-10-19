"""Stock rules: quantity can never go negative, every change is a StockMovement, and the item row is locked while it
changes so two simultaneous sales cannot both succeed on the last unit."""
from decimal import Decimal

from django.db import transaction

from apps.core.models import ActivityLog, Notification
from apps.expenses import services as expense_services
from django.utils import timezone

from .models import InventoryItem, MovementKind, StockMovement


class StockError(Exception):
    """Safe-to-show message."""


def _managers():
    from apps.notifications.services import users_with_permission

    return users_with_permission("inventory.manage")


def low_stock_episode(item):
    """Identifies the current 'running low' episode: it changes whenever stock is replenished, so a reminder is sent
    once per episode rather than on every sale or every scheduled run."""
    last = item.movements.filter(delta__gt=0).order_by("-id").values_list("id", flat=True).first()
    return last or 0


def notify_if_low(item):
    """Create (at most one per episode and user) a low-stock notification. Returns the number created."""
    if not item.is_low:
        return 0
    created = 0
    state = "out of stock" if item.quantity <= 0 else "low"
    for user in _managers():
        note = Notification.send(
            user, "Low stock" if state == "low" else "Out of stock",
            f"{item.name} ({item.sku}): {item.quantity} {item.unit} left, minimum {item.min_stock}.", item.get_absolute_url(),
            kind="low-stock", dedupe_key=f"low-stock:{item.pk}:{low_stock_episode(item)}:{state}")
        created += bool(note)
    return created


@transaction.atomic
def move_stock(item, kind, quantity, user, *, unit_cost=None, supplier=None, reference="", reason="", record_expense=False):
    """Apply a stock movement. `quantity` is a positive number for purchase/usage/initial, and a signed non-zero
    number for adjustments. Returns the StockMovement."""
    item = InventoryItem.objects.select_for_update().get(pk=item.pk)
    if kind not in MovementKind.values:
        raise StockError("Invalid movement type.")
    if not item.is_active and kind != MovementKind.ADJUSTMENT:
        raise StockError("This item is inactive.")
    if not isinstance(quantity, int) or quantity == 0:
        raise StockError("Enter a quantity other than zero.")
    if kind in (MovementKind.PURCHASE, MovementKind.INITIAL):
        if quantity < 0:
            raise StockError("A purchase must be a positive quantity.")
        delta = quantity
    elif kind == MovementKind.USAGE:
        if quantity < 0:
            raise StockError("Usage must be a positive quantity.")
        delta = -quantity
    else:
        delta = quantity
        if not reason.strip():
            raise StockError("A reason is required for stock adjustments.")
    if abs(delta) > 1_000_000:
        raise StockError("That quantity is too large.")
    if unit_cost is not None and unit_cost < 0:
        raise StockError("The price cannot be negative.")
    new_quantity = item.quantity + delta
    if new_quantity < 0:
        raise StockError(f"Only {item.quantity} {item.unit} in stock; this would make the stock negative.")
    movement = StockMovement.objects.create(
        item=item, kind=kind, delta=delta, quantity_after=new_quantity, unit_cost=unit_cost, supplier=supplier or (item.supplier if kind == MovementKind.PURCHASE else None),
        reference=reference[:60], reason=reason.strip()[:255], user=user)
    item.quantity = new_quantity
    update = ["quantity"]
    if kind == MovementKind.PURCHASE and unit_cost is not None:
        item.last_unit_cost = unit_cost
        update.append("last_unit_cost")
    item.save(update_fields=update)
    if kind == MovementKind.PURCHASE and record_expense and unit_cost:
        expense_services.record_expense(
            expense_services.category("Inventory & supplies"), unit_cost * quantity, timezone.localdate(),
            f"Stock purchase: {quantity} × {item.name} ({item.sku})", user, supplier=movement.supplier, reference=reference, stock_movement=movement)
    ActivityLog.record(user, "inventory_movement", f"{movement.get_kind_display()} {delta:+d} {item.unit} – {item.sku} (now {new_quantity})")
    notify_if_low(item)
    return movement


def verify_ledger(item):
    """Audit helper: True when the movement history adds up to the item's current quantity."""
    total = sum(item.movements.values_list("delta", flat=True))
    rows = list(item.movements.order_by("id"))
    running = 0
    for row in rows:
        running += row.delta
        if running != row.quantity_after:
            return False
    return total == item.quantity

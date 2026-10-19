from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import Notification
from apps.core.testing import PASSWORD, make_users
from apps.expenses.models import Expense
from apps.suppliers.models import Supplier

from . import services
from .models import InventoryItem, MovementKind, StockMovement
from .services import StockError


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.staff = self.users["receptionist"]
        self.supplier = Supplier.objects.create(name="Fit Supplies")
        self.item = InventoryItem.objects.create(name="Protein bar", sku="bar-1", category="supplements", min_stock=5, supplier=self.supplier)

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))

    def stock(self, qty):
        return services.move_stock(self.item, MovementKind.PURCHASE, qty, self.staff)


class StockRuleTests(Base):
    def test_sku_is_normalised_and_auto_generated(self):
        self.assertEqual(self.item.sku, "BAR-1")
        auto = InventoryItem.objects.create(name="Towel")
        self.assertEqual(auto.sku, f"SKU-{auto.pk:05d}")
        with self.assertRaises(IntegrityError), transaction.atomic():
            InventoryItem.objects.create(name="Dup", sku="BAR-1")

    def test_purchase_usage_and_adjustment_update_quantity_and_history(self):
        self.stock(20)
        services.move_stock(self.item, MovementKind.USAGE, 6, self.staff, reason="Sold at desk")
        services.move_stock(self.item, MovementKind.ADJUSTMENT, -2, self.staff, reason="Damaged in storage")
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 12)
        rows = list(self.item.movements.order_by("id").values_list("kind", "delta", "quantity_after"))
        self.assertEqual(rows, [("purchase", 20, 20), ("usage", -6, 14), ("adjustment", -2, 12)])
        self.assertTrue(services.verify_ledger(self.item))

    def test_stock_can_never_go_negative(self):
        self.stock(3)
        with self.assertRaisesMessage(StockError, "negative"):
            services.move_stock(self.item, MovementKind.USAGE, 4, self.staff)
        with self.assertRaisesMessage(StockError, "negative"):
            services.move_stock(self.item, MovementKind.ADJUSTMENT, -4, self.staff, reason="Recount")
        self.item.refresh_from_db()
        self.assertEqual((self.item.quantity, self.item.movements.count()), (3, 1))  # failed moves leave no trace

    def test_invalid_movements_are_rejected(self):
        for kind, qty, kwargs, text in (
            (MovementKind.PURCHASE, 0, {}, "other than zero"), (MovementKind.PURCHASE, -5, {}, "positive"), (MovementKind.USAGE, -1, {}, "positive"),
            (MovementKind.ADJUSTMENT, 5, {}, "reason"), (MovementKind.PURCHASE, 2_000_000, {}, "too large"),
            (MovementKind.PURCHASE, 1, {"unit_cost": Decimal("-1")}, "negative"), ("bogus", 1, {}, "Invalid"),
        ):
            with self.assertRaisesMessage(StockError, text):
                services.move_stock(self.item, kind, qty, self.staff, **kwargs)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_database_backstops(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InventoryItem.objects.filter(pk=self.item.pk).update(quantity=-1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(item=self.item, kind="purchase", delta=0, quantity_after=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(item=self.item, kind="purchase", delta=1, quantity_after=-1)

    def test_history_is_append_only(self):
        m = self.stock(5)
        m.reason = "edited"
        with self.assertRaises(ValueError):
            m.save()
        with self.assertRaises(ValueError):
            m.delete()

    def test_inactive_items_cannot_be_sold_but_can_be_counted(self):
        self.stock(5)
        self.item.refresh_from_db()
        self.item.is_active = False
        self.item.save()
        with self.assertRaisesMessage(StockError, "inactive"):
            services.move_stock(self.item, MovementKind.USAGE, 1, self.staff)
        services.move_stock(self.item, MovementKind.ADJUSTMENT, -1, self.staff, reason="Write-off")

    def test_purchase_can_create_exactly_one_linked_expense(self):
        m = services.move_stock(self.item, MovementKind.PURCHASE, 10, self.staff, unit_cost=Decimal("50"), reference="INV-9", record_expense=True)
        expense = Expense.objects.get()
        self.assertEqual((expense.amount, expense.stock_movement, expense.category.name, expense.supplier), (Decimal("500.00"), m, "Inventory & supplies", self.supplier))
        self.item.refresh_from_db()
        self.assertEqual(self.item.last_unit_cost, Decimal("50.00"))
        services.move_stock(self.item, MovementKind.PURCHASE, 5, self.staff, unit_cost=Decimal("60"))  # no expense requested
        self.assertEqual(Expense.objects.count(), 1)


class LowStockTests(Base):
    def test_one_notification_per_low_episode_per_manager(self):
        self.stock(8)
        self.item.refresh_from_db()
        self.assertEqual(Notification.objects.filter(kind="low-stock").count(), 0)
        services.move_stock(self.item, MovementKind.USAGE, 4, self.staff)  # 4 <= 5: low
        first = Notification.objects.filter(kind="low-stock").count()
        recipients = {n.user.role for n in Notification.objects.filter(kind="low-stock")}
        self.assertGreaterEqual(first, 2)
        self.assertEqual(recipients, {"super_admin", "manager", "receptionist"})  # everyone with inventory.manage
        services.move_stock(self.item, MovementKind.USAGE, 1, self.staff)  # still the same episode (state: low)
        self.assertEqual(Notification.objects.filter(kind="low-stock").count(), first)
        self.item.refresh_from_db()
        self.assertEqual(services.notify_if_low(self.item), 0)  # scheduled re-checks do not duplicate either
        services.move_stock(self.item, MovementKind.USAGE, 3, self.staff)  # now zero: "out of stock" is a new state
        self.assertGreater(Notification.objects.filter(kind="low-stock").count(), first)
        before = Notification.objects.filter(kind="low-stock").count()
        self.stock(20)  # replenished
        services.move_stock(self.item, MovementKind.USAGE, 17, self.staff)  # low again: a new episode
        self.assertGreater(Notification.objects.filter(kind="low-stock").count(), before)

    def test_scheduled_scan_finds_items_made_low_by_changing_the_minimum(self):
        self.stock(10)
        self.item.refresh_from_db()
        self.item.min_stock = 20
        self.item.save()
        from apps.notifications import services as scheduled

        self.assertGreater(scheduled.low_stock_alerts(), 0)
        self.assertEqual(scheduled.low_stock_alerts(), 0)

    @override_settings(INVENTORY_ENABLED=False)
    def test_scan_is_disabled_with_the_module(self):
        from apps.notifications import services as scheduled

        self.assertEqual(scheduled.low_stock_alerts(), 0)


class InventoryViewTests(Base):
    def test_item_creation_with_opening_stock(self):
        self.login("manager")
        r = self.client.post(reverse("inventory:create"), {"name": "Towel", "sku": "", "category": "towels", "unit": "pcs", "min_stock": 3, "opening_quantity": 12, "is_active": "on"})
        item = InventoryItem.objects.get(name="Towel")
        self.assertRedirects(r, item.get_absolute_url())
        self.assertEqual((item.quantity, item.movements.get().kind), (12, "initial"))
        self.assertEqual(self.client.post(reverse("inventory:create"), {"name": "Dup", "sku": "bar-1", "category": "other", "unit": "pcs", "min_stock": 0, "opening_quantity": 0}).status_code, 200)

    def test_purchase_usage_and_adjust_views(self):
        self.login("manager")
        r = self.client.post(reverse("inventory:purchase", args=[self.item.pk]), {"quantity": 24, "unit_cost": "40.00", "supplier": self.supplier.pk, "reference": "INV-1", "record_expense": "on"})
        self.assertRedirects(r, self.item.get_absolute_url())
        self.assertEqual(Expense.objects.count(), 1)
        self.assertContains(self.client.post(reverse("inventory:usage", args=[self.item.pk]), {"quantity": 100, "reason": ""}), "negative")
        self.client.post(reverse("inventory:usage", args=[self.item.pk]), {"quantity": 4, "reason": "Sold"})
        r = self.client.post(reverse("inventory:adjust", args=[self.item.pk]), {"counted": 19, "reason": "Stock take"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 19)
        self.assertEqual(self.item.movements.get(kind="adjustment").delta, -1)
        self.assertContains(self.client.post(reverse("inventory:adjust", args=[self.item.pk]), {"counted": 19, "reason": "Same"}), "nothing to adjust")
        self.assertContains(self.client.post(reverse("inventory:purchase", args=[self.item.pk]), {"quantity": 1, "record_expense": "on"}), "price per unit")

    def test_permissions(self):
        for role, view, manage in (("manager", 200, 200), ("receptionist", 200, 200), ("accountant", 200, 403), ("trainer", 403, 403), ("member", 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("inventory:list")).status_code, view, role)
            self.assertEqual(self.client.get(reverse("inventory:detail", args=[self.item.pk])).status_code, view, role)
            self.assertEqual(self.client.get(reverse("inventory:movements")).status_code, view, role)
            self.assertEqual(self.client.get(reverse("inventory:purchase", args=[self.item.pk])).status_code, manage, role)
            usage = self.client.post(reverse("inventory:usage", args=[self.item.pk]), {"quantity": 0})  # 0 is invalid, so 200 = allowed in
            self.assertEqual(usage.status_code, 403 if manage == 403 else 200, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("inventory:list")).status_code, 302)

    def test_receptionist_expense_checkbox_is_hidden_without_expense_permission(self):
        self.login("receptionist")
        form = self.client.get(reverse("inventory:purchase", args=[self.item.pk])).context["form"]
        self.assertNotIn("record_expense", form.fields)
        self.client.post(reverse("inventory:purchase", args=[self.item.pk]), {"quantity": 5, "unit_cost": "10", "record_expense": "on"})
        self.assertFalse(Expense.objects.exists())  # cannot create expenses through the back door

    def test_list_filters_and_low_banner(self):
        other = InventoryItem.objects.create(name="Bottle", category="accessories", min_stock=1)
        services.move_stock(other, MovementKind.PURCHASE, 10, self.staff)
        self.login("manager")
        names = lambda **p: {i.name for i in self.client.get(reverse("inventory:list"), p).context["object_list"]}
        self.assertEqual(names(), {"Protein bar", "Bottle"})
        self.assertEqual(names(stock="low"), {"Protein bar"})
        self.assertEqual(names(q="bott"), {"Bottle"})
        self.assertEqual(names(category="accessories"), {"Bottle"})
        self.assertContains(self.client.get(reverse("inventory:list")), "minimum stock level")

    def test_movement_history_filters(self):
        self.stock(5)
        services.move_stock(self.item, MovementKind.USAGE, 1, self.staff)
        self.login("accountant")
        kinds = lambda **p: [m.kind for m in self.client.get(reverse("inventory:movements"), p).context["object_list"]]
        self.assertEqual(sorted(kinds()), ["purchase", "usage"])
        self.assertEqual(kinds(kind="usage"), ["usage"])
        self.assertEqual(kinds(q="nothing"), [])

    @override_settings(INVENTORY_ENABLED=False)
    def test_optional_module_can_be_switched_off(self):
        self.login("manager")
        for url in (reverse("inventory:list"), reverse("inventory:create"), reverse("inventory:detail", args=[self.item.pk]), reverse("inventory:movements")):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertNotContains(self.client.get(reverse("core:home")), "Low-stock items")

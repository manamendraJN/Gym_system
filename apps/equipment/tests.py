from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Notification
from apps.core.testing import PASSWORD, make_users
from apps.expenses.models import Expense
from apps.suppliers.models import Supplier

from . import services
from .models import Equipment, EquipmentStatus, FaultReport
from .services import EquipmentError

TODAY = timezone.localdate


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.manager = self.users["manager"]
        self.supplier = Supplier.objects.create(name="Gym Gear Co")
        self.eq = Equipment.objects.create(name="Treadmill", code="tm-01", category="cardio", location="Floor 1", maintenance_interval_days=90)

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))

    def maintain(self, **kw):
        data = dict(kind="repair", performed_on=TODAY(), description="Replaced belt", cost=Decimal("1500"), technician="Bob")
        data.update(kw)
        return services.record_maintenance(self.eq, self.manager, **data)


class RegistrationTests(Base):
    def form(self, **kw):
        data = {"name": "Rowing machine", "code": "", "category": "cardio", "serial_number": "SN-1", "location": "Floor 2", "condition": "good", "status": "available",
                "purchase_date": "2026-01-10", "purchase_cost": "250000", "supplier": self.supplier.pk, "warranty_expires": "2028-01-10", "warranty_notes": "2 years",
                "maintenance_interval_days": "180", "next_maintenance_date": "2026-07-10", "notes": ""}
        data.update(kw)
        return data

    def test_register_with_generated_and_explicit_codes(self):
        self.login("manager")
        self.client.post(reverse("equipment:create"), self.form())
        generated = Equipment.objects.get(name="Rowing machine")
        self.assertEqual(generated.code, f"EQ-{generated.pk:04d}")
        r = self.client.post(reverse("equipment:create"), self.form(name="Bike", code="bike-7"))
        self.assertEqual(Equipment.objects.get(name="Bike").code, "BIKE-7")
        self.assertContains(self.client.post(reverse("equipment:create"), self.form(name="Dup", code="TM-01")), "already in use")
        self.assertEqual(Equipment.objects.count(), 3)

    def test_validation(self):
        self.login("manager")
        for bad in (self.form(purchase_date="2999-01-01"), self.form(warranty_expires="2020-01-01"), self.form(purchase_cost="-5")):
            self.assertEqual(self.client.post(reverse("equipment:create"), bad).status_code, 200)

    def test_purchase_expense_option_creates_one_linked_expense(self):
        self.login("manager")
        self.client.post(reverse("equipment:create"), self.form(record_purchase_expense="on"))
        eq = Equipment.objects.get(name="Rowing machine")
        expense = Expense.objects.get()
        self.assertEqual((expense.equipment, expense.amount, expense.category.name), (eq, Decimal("250000.00"), "Equipment"))
        self.client.post(reverse("equipment:edit", args=[eq.pk]), self.form(name="Rowing machine v2", code=eq.code))
        self.assertEqual(Expense.objects.count(), 1)  # editing never creates another
        self.assertContains(self.client.post(reverse("equipment:create"), self.form(name="X", purchase_cost="", record_purchase_expense="on")), "purchase cost")

    def test_expense_option_hidden_without_expense_permission(self):
        self.login("receptionist")  # has equipment.view only
        self.assertEqual(self.client.get(reverse("equipment:create")).status_code, 403)

    def test_permissions(self):
        for role, view, manage in (("manager", 200, 200), ("receptionist", 200, 403), ("trainer", 200, 403), ("accountant", 200, 403), ("member", 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("equipment:list")).status_code, view, role)
            self.assertEqual(self.client.get(self.eq.get_absolute_url()).status_code, view, role)
            self.assertEqual(self.client.get(reverse("equipment:fault", args=[self.eq.pk])).status_code, view, role)  # anyone who sees it can report a fault
            self.assertEqual(self.client.get(reverse("equipment:create")).status_code, manage, role)
            self.assertEqual(self.client.get(reverse("equipment:maintenance_add", args=[self.eq.pk])).status_code, manage, role)
            self.assertEqual(self.client.post(reverse("equipment:status", args=[self.eq.pk]), {"status": "in_use"}).status_code, 302 if manage == 200 else 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("equipment:list")).status_code, 302)

    def test_list_search_and_filters(self):
        other = Equipment.objects.create(name="Dumbbells", code="DB-1", category="free_weights", status="out_of_service",
                                         next_maintenance_date=TODAY() - timedelta(days=3))
        soon = Equipment.objects.create(name="Bench", code="BN-1", next_maintenance_date=TODAY() + timedelta(days=5))
        self.login("trainer")
        names = lambda **p: {e.name for e in self.client.get(reverse("equipment:list"), p).context["object_list"]}
        self.assertEqual(names(q="floor 1"), {"Treadmill"})
        self.assertEqual(names(category="free_weights"), {"Dumbbells"})
        self.assertEqual(names(status="out_of_service"), {"Dumbbells"})
        self.assertEqual(names(maintenance="overdue"), {"Dumbbells"})
        self.assertEqual(names(maintenance="soon"), {"Bench"})
        self.assertEqual(names(q="tm-"), {"Treadmill"})


class FaultTests(Base):
    def test_minor_fault_keeps_equipment_in_service_and_notifies_maintenance_staff(self):
        fault = services.report_fault(self.eq, "Squeaking noise", "minor", self.users["trainer"])
        self.eq.refresh_from_db()
        self.assertEqual((self.eq.status, fault.is_open, fault.reported_by), ("available", True, self.users["trainer"]))
        recipients = {n.user.role for n in Notification.objects.filter(kind="equipment-fault")}
        self.assertEqual(recipients, {"super_admin", "manager"})  # equipment.manage holders, not the reporter
        self.assertEqual(Notification.objects.filter(kind="equipment-fault", dedupe_key=f"equipment-fault:{fault.pk}").count(), 2)

    def test_major_fault_takes_equipment_out_of_service(self):
        services.report_fault(self.eq, "Frame cracked", "major", self.users["trainer"])
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.status, EquipmentStatus.OUT_OF_SERVICE)

    def test_fault_needs_a_description(self):
        with self.assertRaises(EquipmentError):
            services.report_fault(self.eq, "  ", "minor", self.users["trainer"])

    def test_trainer_reports_through_the_view(self):
        self.login("trainer")
        r = self.client.post(reverse("equipment:fault", args=[self.eq.pk]), {"severity": "major", "description": "Handle loose"})
        self.assertRedirects(r, self.eq.get_absolute_url())
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.status, "out_of_service")
        self.assertContains(self.client.get(self.eq.get_absolute_url()), "Handle loose")
        self.assertEqual(self.client.post(reverse("equipment:fault", args=[self.eq.pk]), {"severity": "minor", "description": ""}).status_code, 200)


class MaintenanceTests(Base):
    def test_record_updates_schedule_and_creates_one_linked_expense(self):
        record = self.maintain(supplier=self.supplier)
        self.eq.refresh_from_db()
        self.assertEqual((self.eq.last_maintenance_date, self.eq.next_maintenance_date), (TODAY(), TODAY() + timedelta(days=90)))  # from the interval
        expense = record.expense
        self.assertEqual((expense.amount, expense.category.name, expense.supplier, expense.entered_by), (Decimal("1500.00"), "Maintenance", self.supplier, self.manager))
        self.assertEqual(Expense.objects.count(), 1)

    def test_explicit_next_due_date_wins_and_free_work_has_no_expense(self):
        record = self.maintain(cost=Decimal("0"), next_due=TODAY() + timedelta(days=30), kind="routine")
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.next_maintenance_date, TODAY() + timedelta(days=30))
        self.assertFalse(Expense.objects.exists())
        self.assertFalse(hasattr(record, "expense"))

    def test_cost_is_not_double_counted_when_expense_is_declined(self):
        self.maintain(record_as_expense=False)
        self.assertFalse(Expense.objects.exists())

    def test_repair_resolves_fault_and_returns_to_service(self):
        fault = services.report_fault(self.eq, "Belt slipping", "major", self.users["trainer"])
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.status, "out_of_service")
        self.maintain(fault=fault, return_to_service=True, condition_after="excellent")
        self.eq.refresh_from_db()
        fault.refresh_from_db()
        self.assertEqual((self.eq.status, self.eq.condition, fault.is_open, fault.resolved_by), ("available", "excellent", False, self.manager))
        with self.assertRaisesMessage(EquipmentError, "already resolved"):
            self.maintain(fault=fault)

    def test_validation_rules(self):
        other = Equipment.objects.create(name="Other", code="OT-1")
        foreign = FaultReport.objects.create(equipment=other, description="x")
        for kwargs, text in (({"performed_on": TODAY() + timedelta(days=1)}, "future"), ({"cost": Decimal("-1")}, "negative"),
                             ({"fault": foreign}, "another item"), ({"next_due": TODAY() - timedelta(days=1)}, "after the service")):
            with self.assertRaisesMessage(EquipmentError, text):
                self.maintain(**kwargs)
        self.assertEqual(self.eq.maintenance.count(), 0)

    def test_rolls_back_completely_on_failure(self):
        from unittest import mock

        with mock.patch("apps.equipment.services.ActivityLog.record", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.maintain()
        self.assertEqual((self.eq.maintenance.count(), Expense.objects.count()), (0, 0))

    def test_history_is_permanent(self):
        record = self.maintain()
        with self.assertRaises(ValueError):
            record.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(record).objects.filter(pk=record.pk).update(cost=Decimal("-1"))

    def test_status_workflow(self):
        self.login("manager")
        for status in ("maintenance", "in_use", "out_of_service", "available"):
            self.client.post(reverse("equipment:status", args=[self.eq.pk]), {"status": status})
            self.eq.refresh_from_db()
            self.assertEqual(self.eq.status, status)
        self.client.post(reverse("equipment:status", args=[self.eq.pk]), {"status": "bogus"})
        self.eq.refresh_from_db()
        self.assertEqual(self.eq.status, "available")
        self.assertEqual(self.client.get(reverse("equipment:status", args=[self.eq.pk])).status_code, 405)

    def test_view_flow_and_cost_totals(self):
        self.login("manager")
        data = {"kind": "repair", "performed_on": TODAY().isoformat(), "description": "New motor", "cost": "4000", "technician": "ACME", "next_due": "", "condition_after": "", "record_as_expense": "on"}
        r = self.client.post(reverse("equipment:maintenance_add", args=[self.eq.pk]), data)
        self.assertRedirects(r, self.eq.get_absolute_url())
        self.client.post(reverse("equipment:maintenance_add", args=[self.eq.pk]), {**data, "kind": "routine", "cost": "500", "description": "Service"})
        page = self.client.get(self.eq.get_absolute_url())
        self.assertEqual((page.context["total_cost"], page.context["repair_cost"]), (Decimal("4500.00"), Decimal("4000.00")))
        listing = self.client.get(reverse("equipment:maintenance_list"))
        self.assertEqual(listing.context["total_cost"], Decimal("4500.00"))
        self.assertEqual(len(listing.context["object_list"]), 2)
        self.assertEqual(self.client.get(reverse("equipment:maintenance_list"), {"kind": "repair"}).context["total_cost"], Decimal("4000.00"))
        self.assertEqual(self.client.post(reverse("equipment:maintenance_add", args=[self.eq.pk]), {**data, "cost": "-3"}).status_code, 200)


class MaintenanceReminderTests(Base):
    def run_reminders(self, today):
        from apps.notifications import services as scheduled

        return scheduled.equipment_maintenance_reminders(today)

    def test_reminders_by_period_without_duplicates_and_rearm_after_service(self):
        self.eq.next_maintenance_date = TODAY() + timedelta(days=5)
        self.eq.save()
        staff_count = Notification.objects.none().count()
        first = self.run_reminders(TODAY())
        self.assertEqual(first, 2)  # super admin + manager (equipment.manage), band "7 days"
        self.assertEqual(self.run_reminders(TODAY()), 0)  # same run again: nothing new
        self.assertEqual(self.run_reminders(TODAY() + timedelta(days=1)), 0)  # still inside the 7-day band
        self.assertEqual(self.run_reminders(TODAY() + timedelta(days=4)), 2)  # 1 day left: next band
        self.assertEqual(self.run_reminders(TODAY() + timedelta(days=5)), 2)  # due today: final band
        overdue = self.run_reminders(TODAY() + timedelta(days=9))
        self.assertEqual(overdue, 0)  # overdue stays in the last band: no repeat nagging
        self.assertTrue(Notification.objects.filter(kind="equipment-maintenance", message__contains="due today").exists())
        self.maintain(next_due=TODAY() + timedelta(days=40), cost=Decimal("0"))
        self.assertEqual(self.run_reminders(TODAY() + timedelta(days=36)), 2)  # a new cycle starts a new set of reminders

    def test_equipment_without_a_due_date_is_ignored(self):
        self.assertEqual(self.run_reminders(TODAY()), 0)

    def test_overdue_equipment_is_reported_once(self):
        self.eq.next_maintenance_date = TODAY() - timedelta(days=3)
        self.eq.save()
        self.assertEqual(self.run_reminders(TODAY()), 2)
        self.assertEqual(self.run_reminders(TODAY()), 0)
        self.assertTrue(Notification.objects.filter(message__contains="overdue").exists())

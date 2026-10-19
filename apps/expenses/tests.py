import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import ActivityLog
from apps.core.testing import PASSWORD, make_users
from apps.suppliers.models import Supplier

from . import services
from .models import Expense, ExpenseCategory
from .services import ExpenseError

PRIVATE = tempfile.mkdtemp()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.rent = ExpenseCategory.objects.get(name="Rent")
        self.supplier = Supplier.objects.create(name="Landlord Ltd")

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))

    def expense(self, amount="1000", **kw):
        kw.setdefault("category", self.rent)
        return services.record_expense(kw.pop("category"), Decimal(amount), kw.pop("expense_date", timezone.localdate()),
                                       kw.pop("description", "October rent"), kw.pop("user", self.users["accountant"]), **kw)

    def payload(self, **kw):
        data = {"category": self.rent.pk, "amount": "25000.00", "expense_date": timezone.localdate().isoformat(), "description": "Rent for October",
                "supplier": self.supplier.pk, "reference": "INV-77"}
        data.update(kw)
        return data


class CategoryTests(Base):
    def test_default_categories_are_seeded(self):
        names = set(ExpenseCategory.objects.values_list("name", flat=True))
        for expected in ("Rent", "Electricity", "Water", "Salaries", "Equipment", "Maintenance", "Cleaning supplies"):
            self.assertIn(expected, names)

    def test_category_management_and_uniqueness(self):
        self.login("accountant")
        self.assertEqual(self.client.post(reverse("expenses:category_create"), {"name": "Insurance", "is_active": "on"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("expenses:category_create"), {"name": "insurance"}).status_code, 200)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExpenseCategory.objects.create(name="RENT")
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("expenses:categories")).status_code, 403)

    def test_inactive_categories_are_not_offered_for_new_expenses(self):
        ExpenseCategory.objects.filter(name="Water").update(is_active=False)
        self.login("accountant")
        names = {c.name for c in self.client.get(reverse("expenses:create")).context["form"].fields["category"].queryset}
        self.assertNotIn("Water", names)


class RecordExpenseTests(Base):
    def test_accountant_records_expense_with_receipt(self):
        self.login("accountant")
        r = self.client.post(reverse("expenses:create"), {**self.payload(), "receipt": SimpleUploadedFile("rent.pdf", PDF)})
        e = Expense.objects.get()
        self.assertRedirects(r, e.get_absolute_url())
        self.assertEqual((e.amount, e.entered_by, e.supplier, e.reference), (Decimal("25000.00"), self.users["accountant"], self.supplier, "INV-77"))
        self.assertTrue(e.receipt.path.startswith(PRIVATE))  # outside the public media folder
        self.assertTrue(ActivityLog.objects.filter(action="expense_recorded").exists())

    def test_validation(self):
        self.login("accountant")
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        for bad in (self.payload(amount="0"), self.payload(amount="-5"), self.payload(expense_date=tomorrow), self.payload(description="x"),
                    {**self.payload(), "receipt": SimpleUploadedFile("a.pdf", b"not a pdf")}, {**self.payload(), "receipt": SimpleUploadedFile("a.exe", b"MZ")},
                    {**self.payload(), "receipt": SimpleUploadedFile("big.pdf", PDF + b"0" * (6 * 1024 * 1024))}):
            self.assertEqual(self.client.post(reverse("expenses:create"), bad).status_code, 200)
        self.assertFalse(Expense.objects.exists())

    def test_database_rejects_non_positive_amounts(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Expense.objects.create(category=self.rent, amount=Decimal("0"), description="x")

    def test_permissions(self):
        e = self.expense()
        for role, view, manage, void in (("manager", 200, 200, 200), ("super_admin", 200, 200, 200), ("accountant", 200, 200, 403),
                                         ("receptionist", 403, 403, 403), ("trainer", 403, 403, 403), ("member", 403, 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("expenses:list")).status_code, view, role)
            self.assertEqual(self.client.get(e.get_absolute_url()).status_code, view, role)
            self.assertEqual(self.client.get(reverse("expenses:create")).status_code, manage, role)
            self.assertEqual(self.client.get(reverse("expenses:edit", args=[e.pk])).status_code, manage, role)
            self.assertEqual(self.client.get(reverse("expenses:void", args=[e.pk])).status_code, void, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("expenses:list")).status_code, 302)
        # financial figures are not leaked in the dashboard to roles without expenses.view
        self.login("receptionist")
        self.assertNotContains(self.client.get(reverse("core:home")), "Expenses this month")

    def test_edit_logs_the_amount_change(self):
        e = self.expense("1000")
        self.login("accountant")
        r = self.client.post(reverse("expenses:edit", args=[e.pk]), self.payload(amount="1200"))
        self.assertRedirects(r, e.get_absolute_url())
        e.refresh_from_db()
        self.assertEqual(e.amount, Decimal("1200.00"))
        self.assertTrue(ActivityLog.objects.filter(action="expense_updated", description__contains="1,000.00 → 1,200.00").exists())

    def test_cannot_delete_expenses(self):
        with self.assertRaises(ValueError):
            self.expense().delete()


class VoidTests(Base):
    def test_void_requires_reason_and_removes_from_totals(self):
        from apps.reports import finance

        e = self.expense("1000")
        self.expense("500")
        today = timezone.localdate()
        self.assertEqual(finance.expenses_total(today, today), Decimal("1500.00"))
        self.login("manager")
        self.assertContains(self.client.post(reverse("expenses:void", args=[e.pk]), {"reason": " "}), "reason")
        r = self.client.post(reverse("expenses:void", args=[e.pk]), {"reason": "Entered twice"})
        self.assertRedirects(r, e.get_absolute_url())
        e.refresh_from_db()
        self.assertEqual((e.is_void, e.voided_by, e.void_reason), (True, self.users["manager"], "Entered twice"))
        self.assertEqual(finance.expenses_total(today, today), Decimal("500.00"))
        self.assertEqual(Expense.objects.count(), 2)  # the record is kept
        with self.assertRaisesMessage(ExpenseError, "already void"):
            services.void_expense(e, self.users["manager"], "again")
        self.assertEqual(self.client.get(reverse("expenses:edit", args=[e.pk])).status_code, 404)  # voided entries are frozen

    def test_linked_expenses_cannot_be_voided_or_edited_here(self):
        from apps.equipment.models import Equipment
        from apps.equipment import services as equipment_services

        eq = Equipment.objects.create(name="Treadmill", code="TM-1")
        record = equipment_services.record_maintenance(eq, self.users["manager"], kind="repair", performed_on=timezone.localdate(), description="Belt", cost=Decimal("800"))
        expense = record.expense
        self.assertTrue(expense.is_linked)
        with self.assertRaisesMessage(ExpenseError, "maintenance record"):
            services.void_expense(expense, self.users["manager"], "mistake")
        self.login("manager")
        self.assertEqual(self.client.get(reverse("expenses:edit", args=[expense.pk])).status_code, 404)
        self.assertNotContains(self.client.get(expense.get_absolute_url()), reverse("expenses:void", args=[expense.pk]))


class ReceiptTests(Base):
    def test_receipt_download_is_permission_checked_private_and_logged(self):
        self.login("accountant")
        self.client.post(reverse("expenses:create"), {**self.payload(), "receipt": SimpleUploadedFile("rent.pdf", PDF)})
        e = Expense.objects.get()
        url = reverse("expenses:receipt", args=[e.pk])
        r = self.client.get(url)
        self.assertEqual((r.status_code, b"".join(r.streaming_content)), (200, PDF))
        self.assertIn("no-store", r["Cache-Control"])
        self.assertTrue(ActivityLog.objects.filter(action="expense_receipt_viewed").exists())
        for role in ("receptionist", "trainer", "member"):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        with self.assertRaises(ValueError):
            e.receipt.url  # never exposed as a public URL
        no_file = self.expense()
        self.login("manager")
        self.assertEqual(self.client.get(reverse("expenses:receipt", args=[no_file.pk])).status_code, 404)


class ListTests(Base):
    def test_search_filters_totals_and_pagination(self):
        today = timezone.localdate()
        elec = ExpenseCategory.objects.get(name="Electricity")
        a = self.expense("1000", description="October rent", supplier=self.supplier, reference="R-1")
        b = self.expense("250.50", category=elec, description="Power bill", expense_date=today - timedelta(days=40))
        v = self.expense("999", description="Mistake")
        services.void_expense(v, self.users["manager"], "Wrong")
        self.login("accountant")
        ids = lambda **p: {e.pk for e in self.client.get(reverse("expenses:list"), p).context["object_list"]}
        self.assertEqual(ids(), {a.pk, b.pk})  # void hidden by default
        self.assertEqual(ids(show="all"), {a.pk, b.pk, v.pk})
        self.assertEqual(ids(show="void"), {v.pk})
        self.assertEqual(ids(q="power"), {b.pk})
        self.assertEqual(ids(q="R-1"), {a.pk})
        self.assertEqual(ids(q="landlord"), {a.pk})
        self.assertEqual(ids(category=elec.pk), {b.pk})
        self.assertEqual(ids(supplier=self.supplier.pk), {a.pk})
        self.assertEqual(ids(**{"from": (today - timedelta(days=10)).isoformat()}), {a.pk})
        self.assertEqual(ids(to=(today - timedelta(days=10)).isoformat()), {b.pk})
        self.assertEqual(self.client.get(reverse("expenses:list")).context["total"], Decimal("1250.50"))
        self.assertEqual(self.client.get(reverse("expenses:list"), {"sort": "amount", "dir": "desc"}).context["object_list"][0].pk, a.pk)
        for i in range(25):
            self.expense("10", description=f"Bulk {i}")
        r = self.client.get(reverse("expenses:list"))
        self.assertEqual(len(r.context["object_list"]), 20)
        self.assertEqual(len(self.client.get(reverse("expenses:list"), {"page": 2}).context["object_list"]), 7)

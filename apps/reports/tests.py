import csv
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.attendance import services as attendance_services
from apps.core.models import ActivityLog
from apps.core.stats import get_dashboard_stats
from apps.core.testing import PASSWORD, future_slot, give_membership, make_member_with_login, make_trainer, make_users
from apps.equipment import services as equipment_services
from apps.equipment.models import Equipment
from apps.expenses import services as expense_services
from apps.expenses.models import ExpenseCategory
from apps.inventory import services as inventory_services
from apps.inventory.models import InventoryItem
from apps.appointments import services as appointment_services
from apps.members.tests import make_member
from apps.memberships import services as membership_services
from apps.memberships.models import MembershipPlan
from apps.payments import services as pay
from apps.trainers import services as trainer_services

from . import finance
from .definitions import REGISTRY, REPORTS

D = Decimal
TODAY = timezone.localdate


class Scenario(TestCase):
    """A small gym with known figures so every total can be checked by hand.

    Memberships (price 5000, start today):  Alice paid 5000 cash, then 500 refunded.  Bob got a 300 discount and paid 2000 by bank transfer;
    a further 1000 transfer is still pending (not counted).
    Expenses: rent 20000 (manual), maintenance 1500 (linked), stock purchase 10 x 50 = 500 (linked), a void 999.
    """

    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.manager, self.accountant, self.staff = self.users["manager"], self.users["accountant"], self.users["receptionist"]
        self.plan = MembershipPlan.objects.create(name="Monthly", price=D("5000"))
        self.alice, self.alice_login = make_member_with_login(self.users, "Alice Member", membership=False)
        self.bob, self.bob_login = make_member_with_login(self.users, "Bob Member", membership=False)
        self.m_alice = membership_services.assign_membership(self.alice, self.plan, TODAY(), self.manager)
        self.m_bob = membership_services.assign_membership(self.bob, self.plan, TODAY(), self.manager)
        self.p_alice, _ = pay.record_payment(self.m_alice, D("5000"), "cash", self.staff, "a1")
        pay.refund_payment(self.p_alice, D("500"), "cash", self.manager, "a2", "Goodwill")
        pay.adjust_charge(self.m_bob, D("-300"), self.manager, "b1", "Loyalty discount")
        pay.record_payment(self.m_bob, D("2000"), "bank_transfer", self.staff, "b2", external_reference="SLIP-1")
        pay.record_payment(self.m_bob, D("1000"), "bank_transfer", self.staff, "b3", pending=True)
        rent = ExpenseCategory.objects.get(name="Rent")
        self.rent = expense_services.record_expense(rent, D("20000"), TODAY(), "Monthly rent", self.accountant)
        self.eq = Equipment.objects.create(name="Treadmill", code="TM-1")
        self.record = equipment_services.record_maintenance(self.eq, self.manager, kind="repair", performed_on=TODAY(), description="New belt", cost=D("1500"))
        self.item = InventoryItem.objects.create(name="Protein bar", min_stock=5)
        inventory_services.move_stock(self.item, "purchase", 10, self.manager, unit_cost=D("50"), record_expense=True)
        self.voided = expense_services.record_expense(rent, D("999"), TODAY(), "Entered by mistake", self.accountant)
        expense_services.void_expense(self.voided, self.manager, "Mistake")

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def params(self):
        return {"from": TODAY().replace(day=1).isoformat(), "to": TODAY().isoformat()}


class FinancialRulesTests(Scenario):
    def test_collected_refunds_and_revenue(self):
        t = finance.totals(TODAY(), TODAY())
        self.assertEqual((t["gross"], t["refunds"], t["net"]), (D("7000"), D("500"), D("6500")))  # pending 1000 is not collected

    def test_expenses_count_each_cost_once_and_exclude_void(self):
        self.assertEqual(finance.expenses_total(TODAY(), TODAY()), D("22000"))  # 20000 + 1500 (maintenance) + 500 (stock); the 999 is void
        self.assertEqual(self.record.expense.amount, D("1500"))
        self.assertEqual(self.record.cost, D("1500"))  # the record's own cost field is informational, never added again

    def test_invoiced_is_what_was_billed_not_what_was_paid(self):
        self.assertEqual(finance.invoiced(TODAY(), TODAY()), D("9700"))  # 5000 + 5000 - 300 discount
        self.assertEqual(finance.invoiced(TODAY() - timedelta(days=60), TODAY() - timedelta(days=30)), D("0"))

    def test_outstanding_is_a_receivable_separate_from_revenue(self):
        self.assertEqual(finance.outstanding_now(), D("3200"))  # Alice 5000-4500=500, Bob 4700-2000=2700
        membership_services.cancel_membership(self.m_bob, self.manager, "Left")
        self.assertEqual(finance.outstanding_now(), D("500"))  # cancelling writes off the unpaid balance

    def test_estimated_operating_profit(self):
        s = finance.summary(TODAY(), TODAY())
        self.assertEqual((s["net"], s["expenses"], s["profit"]), (D("6500"), D("22000"), D("-15500")))
        self.assertEqual((s["invoiced"], s["outstanding"]), (D("9700"), D("3200")))

    def test_adjustments_and_pending_payments_do_not_change_revenue(self):
        before = finance.totals(TODAY(), TODAY())["net"]
        pay.adjust_charge(self.m_bob, D("-100"), self.manager, "b9", "Extra discount")
        self.assertEqual(finance.totals(TODAY(), TODAY())["net"], before)

    def test_voiding_a_payment_removes_it_from_revenue_once(self):
        p, _ = pay.record_payment(self.m_bob, D("700"), "cash", self.staff, "b4")
        self.assertEqual(finance.totals(TODAY(), TODAY())["net"], D("7200"))
        pay.void_payment(p, self.manager, "Wrong member")
        self.assertEqual(finance.totals(TODAY(), TODAY())["net"], D("6500"))

    def test_periods_are_respected(self):
        yesterday = TODAY() - timedelta(days=1)
        self.assertEqual(finance.totals(yesterday, yesterday)["net"], D("0"))
        series = finance.monthly_series(TODAY().replace(day=1) - timedelta(days=40), TODAY())
        self.assertEqual(series[-1]["revenue"], D("6500"))
        self.assertEqual((series[-1]["expenses"], series[-1]["profit"]), (D("22000"), D("-15500")))
        self.assertEqual(series[0]["revenue"], D("0"))
        self.assertGreaterEqual(len(series), 2)
        self.assertEqual([r["month"] for r in series], sorted(r["month"] for r in series))


class ReportContentTests(Scenario):
    def report(self, key, user=None, **params):
        self.login(user or self.manager)
        return self.client.get(reverse("reports:report", args=[key]), {**self.params(), **params})

    def rows(self, key, **params):
        return self.report(key, **params).context["page_obj"].object_list

    def test_member_registrations(self):
        r = self.report("member-registrations")
        self.assertEqual({x["name"] for x in r.context["page_obj"].object_list}, {"Alice Member", "Bob Member"})
        self.assertEqual(dict((l, v) for l, v, _ in r.context["summary"])["Members registered"], 2)
        yesterday = (TODAY() - timedelta(days=1)).isoformat()
        self.assertEqual(self.report("member-registrations", **{"from": yesterday, "to": yesterday}).context["total_rows"], 0)

    def test_membership_status_snapshot_and_filter(self):
        r = self.report("membership-status")
        self.assertEqual(dict((l, v) for l, v, _ in r.context["summary"])["Active"], 2)
        self.assertEqual(self.report("membership-status", status="expired").context["total_rows"], 0)
        membership_services.cancel_membership(self.m_bob, self.manager, "Left")
        self.assertEqual(self.report("membership-status", status="cancelled").context["total_rows"], 1)

    def test_renewals_and_plan_popularity(self):
        renewal = membership_services.renew_membership(self.m_alice, self.manager)
        later = (TODAY() + timedelta(days=45)).isoformat()
        r = self.report("renewals", **{"from": TODAY().isoformat(), "to": later})
        self.assertEqual([x["name"] for x in r.context["page_obj"].object_list], ["Alice Member"])
        self.assertEqual(dict((l, v) for l, v, _ in r.context["summary"])["Value of renewals (LKR)"], D("5000"))
        rows = self.report("plan-popularity", **{"from": TODAY().isoformat(), "to": later}).context["page_obj"].object_list
        self.assertEqual((rows[0]["plan"], rows[0]["started"], rows[0]["renewals"], rows[0]["invoiced"]), ("Monthly", 3, 1, D("15000")))
        self.assertEqual(rows[0]["share"], 100)

    def test_attendance_daily_and_monthly(self):
        now = timezone.now()  # all within seconds of each other, so the test cannot straddle midnight
        v1 = attendance_services.check_in(self.alice, self.staff, now=now)
        attendance_services.check_out(v1, self.staff, now=now + timedelta(seconds=1))
        attendance_services.check_in(self.alice, self.staff, now=now + timedelta(seconds=2), method="qr")
        attendance_services.check_in(self.bob, self.staff, now=now)
        daily = self.rows("attendance", group="day")
        self.assertEqual((len(daily), daily[0]["visits"], daily[0]["members"], daily[0]["qr"]), (1, 3, 2, 1))
        monthly = self.rows("attendance", group="month")
        self.assertEqual((monthly[0]["period"], monthly[0]["visits"]), (TODAY().strftime("%b %Y"), 3))

    def test_member_attendance_history(self):
        now = timezone.now()
        attendance_services.check_in(self.alice, self.staff, now=now)
        r = self.report("member-attendance", member=self.alice.member_id)
        self.assertEqual(r.context["total_rows"], 1)
        self.assertEqual(self.report("member-attendance", member=self.alice.phone).context["total_rows"], 1)
        self.assertEqual(self.report("member-attendance", member="GM999999").context["total_rows"], 0)
        self.assertEqual(self.report("member-attendance").context["total_rows"], 0)  # nothing chosen yet

    def test_payments_daily_and_monthly(self):
        row = self.rows("payments", group="day")[0]
        self.assertEqual((row["count"], row["gross"], row["refunds"], row["net"]), (2, D("7000"), D("500"), D("6500")))
        self.assertEqual(self.rows("payments", group="month")[0]["net"], D("6500"))
        summary = dict((l, v) for l, v, _ in self.report("payments").context["summary"])
        self.assertEqual(summary["Awaiting confirmation (not counted)"], D("1000"))

    def test_outstanding_report_lists_memberships_and_sessions(self):
        trainer = make_trainer("Coach", rate="3000")
        trainer_services.assign_trainer(trainer, self.alice, self.manager)
        appointment_services.book(self.alice, trainer, future_slot(3), 60, self.manager)
        rows = self.rows("outstanding")
        self.assertEqual(sorted(r["type"] for r in rows), ["Membership", "Membership", "Training session"])
        self.assertEqual(sorted(r["outstanding"] for r in rows), [D("500"), D("2700"), D("3000")])
        total = dict((l, v) for l, v, _ in self.report("outstanding").context["summary"])["Total outstanding (not revenue)"]
        self.assertEqual(total, D("6200"))

    def test_revenue_report_separates_the_concepts(self):
        s = dict((l, v) for l, v, _ in self.report("revenue").context["summary"])
        self.assertEqual((s["Invoiced in period"], s["Collected (received)"], s["Refunds"], s["Revenue (net collected)"], s["Outstanding now (receivable)"]),
                         (D("9700"), D("7000"), D("500"), D("6500"), D("3200")))
        by_method = {r["method"]: r for r in self.rows("revenue")}
        self.assertEqual((by_method["Cash"]["net"], by_method["Bank transfer"]["net"], by_method["Card"]["net"]), (D("4500"), D("2000"), D("0")))
        self.assertEqual(sum(r["net"] for r in by_method.values()), D("6500"))  # methods add up to revenue: nothing double counted

    def test_expense_and_revenue_vs_expense_reports(self):
        r = self.report("expenses")
        self.assertEqual(r.context["total_rows"], 3)  # rent, maintenance, stock; the void is excluded
        summary = dict((l, v) for l, v, _ in r.context["summary"])
        self.assertEqual((summary["Total expenses"], summary["• Rent"], summary["• Maintenance"], summary["• Inventory & supplies"]), (D("22000"), D("20000"), D("1500"), D("500")))
        self.assertEqual({x["source"] for x in r.context["page_obj"].object_list}, {"Manual", "Maintenance record", "Stock purchase"})
        r = self.report("revenue-vs-expense")
        self.assertEqual(dict((l, v) for l, v, _ in r.context["summary"]), {"Revenue collected": D("6500"), "Expenses": D("22000"), "Estimated operating profit": D("-15500")})
        row = r.context["page_obj"].object_list[0]
        self.assertEqual((row["revenue"], row["expenses"], row["profit"]), (D("6500"), D("22000"), D("-15500")))

    def test_appointment_report(self):
        trainer = make_trainer("Coach", rate="3000")
        a = appointment_services.book(self.alice, trainer, future_slot(3), 60, self.manager)
        appointment_services.book(self.bob, trainer, future_slot(4), 60, self.manager)
        appointment_services.cancel(a, self.manager, "Illness")
        far = (TODAY() + timedelta(days=10)).isoformat()
        r = self.report("appointments", **{"to": far})
        s = dict((l, v) for l, v, _ in r.context["summary"])
        self.assertEqual((s["Scheduled"], s["Cancelled"], r.context["total_rows"]), (1, 1, 2))
        self.assertEqual(self.report("appointments", status="cancelled", to=far).context["total_rows"], 1)

    def test_equipment_and_inventory_reports(self):
        r = self.report("equipment-maintenance")
        s = dict((l, v) for l, v, _ in r.context["summary"])
        self.assertEqual((s["Records"], s["Total maintenance cost"], s["…of which repairs"]), (1, D("1500"), D("1500")))
        inventory_services.move_stock(self.item, "usage", 8, self.manager)  # 10 - 8 = 2 <= minimum 5
        r = self.report("inventory")
        row = r.context["page_obj"].object_list[0]
        self.assertEqual((row["quantity"], row["state"], row["bought"], row["used"], row["value"]), (2, "Low", 10, 8, D("100")))
        self.assertEqual(dict((l, v) for l, v, _ in r.context["summary"])["Low or out of stock"], 1)
        self.assertEqual(self.report("inventory", status="ok").context["total_rows"], 0)
        self.assertEqual(self.report("inventory", status="low").context["total_rows"], 1)

    @override_settings(INVENTORY_ENABLED=False)
    def test_inventory_report_disappears_when_the_module_is_off(self):
        self.assertEqual(self.report("inventory").status_code, 404)
        self.assertNotIn("inventory", [r.key for r in self.client.get(reverse("reports:index")).context["reports"]])


class ReportFeatureTests(Scenario):
    def test_every_report_renders_in_html_print_csv_and_pdf(self):
        self.login(self.manager)
        for cls in REPORTS:
            url = reverse("reports:report", args=[cls.key])
            html = self.client.get(url, self.params())
            self.assertEqual(html.status_code, 200, cls.key)
            self.assertContains(html, cls.title)
            printable = self.client.get(url, {**self.params(), "print": 1})
            self.assertEqual(printable.status_code, 200, cls.key)
            self.assertContains(printable, "window.print()")
            csv_r = self.client.get(url, {**self.params(), "export": "csv"})
            self.assertEqual((csv_r.status_code, csv_r["Content-Type"]), (200, "text/csv; charset=utf-8"), cls.key)
            self.assertIn(".csv", csv_r["Content-Disposition"])
            pdf = self.client.get(url, {**self.params(), "export": "pdf"})
            self.assertEqual((pdf.status_code, pdf["Content-Type"]), (200, "application/pdf"), cls.key)
            self.assertTrue(pdf.content.startswith(b"%PDF"), cls.key)

    def test_csv_contents_match_the_screen(self):
        self.login(self.manager)
        r = self.client.get(reverse("reports:report", args=["expenses"]), {**self.params(), "export": "csv"})
        text = r.content.decode("utf-8-sig")
        rows = list(csv.reader(StringIO(text)))
        self.assertEqual(rows[0], ["Expenses"])
        header_at = next(i for i, row in enumerate(rows) if row[:2] == ["Date", "Category"])
        body = rows[header_at + 1:]
        self.assertEqual(len(body), 3)
        self.assertEqual(sum(D(row[5].replace(",", "")) for row in body), D("22000.00"))
        self.assertIn(["Total expenses", "22,000.00"], rows)
        self.assertTrue(ActivityLog.objects.filter(action="report_exported", description__contains="Expenses (CSV)").exists())

    def test_csv_neutralises_spreadsheet_formulas(self):
        evil = make_member(full_name='=HYPERLINK("http://evil.example","x")')
        self.login(self.manager)
        text = self.client.get(reverse("reports:report", args=["member-registrations"]), {**self.params(), "export": "csv"}).content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)

    def test_search_date_range_and_pagination(self):
        for i in range(30):
            expense_services.record_expense(ExpenseCategory.objects.get(name="Water"), D("10"), TODAY(), f"Water bill {i}", self.accountant)
        self.login(self.manager)
        url = reverse("reports:report", args=["expenses"])
        r = self.client.get(url, self.params())
        self.assertEqual((r.context["total_rows"], len(r.context["page_obj"].object_list), r.context["page_obj"].paginator.num_pages), (33, 25, 2))
        self.assertEqual(len(self.client.get(url, {**self.params(), "page": 2}).context["page_obj"].object_list), 8)
        self.assertEqual(self.client.get(url, {**self.params(), "q": "water bill 7"}).context["total_rows"], 1)
        self.assertEqual(self.client.get(url, {**self.params(), "q": "WATER"}).context["total_rows"], 30)  # case-insensitive
        last_month = TODAY().replace(day=1) - timedelta(days=1)
        self.assertEqual(self.client.get(url, {"from": last_month.replace(day=1).isoformat(), "to": last_month.isoformat()}).context["total_rows"], 0)
        swapped = self.client.get(url, {"from": TODAY().isoformat(), "to": (TODAY() - timedelta(days=3)).isoformat()})
        self.assertEqual(swapped.status_code, 200)  # reversed dates are corrected, not an error
        self.assertEqual(self.client.get(url, {"from": "garbage", "to": "nonsense"}).status_code, 200)
        self.assertEqual(self.client.get(url, {**self.params(), "export": "csv"}).content.decode("utf-8-sig").count("Water bill"), 30)  # exports include every page

    def test_unknown_report_is_404(self):
        self.login(self.manager)
        self.assertEqual(self.client.get(reverse("reports:report", args=["nope"])).status_code, 404)


class ReportPermissionTests(Scenario):
    def status(self, user, key):
        self.login(user)
        return self.client.get(reverse("reports:report", args=[key])).status_code

    def test_financial_reports_are_restricted(self):
        # accountant: reports.view + payments/expenses/members/memberships/equipment/inventory view, but no attendance or appointments
        for key, expected in (("expenses", 200), ("revenue-vs-expense", 200), ("revenue", 200), ("payments", 200), ("outstanding", 200), ("member-registrations", 200),
                              ("equipment-maintenance", 200), ("inventory", 200), ("attendance", 403), ("appointments", 403)):
            self.assertEqual(self.status(self.accountant, key), expected, key)
        for role in ("receptionist", "trainer", "member"):  # none of them hold reports.view
            for cls in REPORTS:
                self.assertEqual(self.status(self.users[role], cls.key), 403, (role, cls.key))
            self.login(self.users[role])
            self.assertEqual(self.client.get(reverse("reports:index")).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("reports:index")).status_code, 302)
        for fmt in ("csv", "pdf"):  # exports obey the same rules
            self.login(self.users["receptionist"])
            self.assertEqual(self.client.get(reverse("reports:report", args=["expenses"]), {"export": fmt}).status_code, 403)

    def test_removing_a_module_permission_hides_its_reports(self):
        from apps.accounts.models import RolePermission

        RolePermission.objects.filter(role="accountant", permission="expenses.view").delete()
        self.assertEqual(self.status(self.accountant, "expenses"), 403)
        self.assertEqual(self.status(self.accountant, "revenue-vs-expense"), 403)
        self.login(self.accountant)
        keys = [r.key for r in self.client.get(reverse("reports:index")).context["reports"]]
        self.assertNotIn("expenses", keys)
        self.assertIn("payments", keys)

    def test_index_lists_everything_for_the_manager(self):
        self.login(self.manager)
        self.assertEqual(len(self.client.get(reverse("reports:index")).context["reports"]), len(REPORTS))
        self.assertContains(self.client.get(reverse("reports:index")), "How the money figures are calculated")
        self.assertEqual(len(REGISTRY), len(REPORTS))


class DashboardOperationsTests(Scenario):
    def test_manager_sees_real_operational_and_financial_figures(self):
        inventory_services.move_stock(self.item, "usage", 8, self.manager)
        self.eq.next_maintenance_date = TODAY() + timedelta(days=3)
        self.eq.save()
        stats = get_dashboard_stats(user=self.manager)
        self.assertEqual(stats["expense_stats"]["month"], D("22000"))
        self.assertEqual(stats["expense_stats"]["profit"], D("-15500"))
        self.assertEqual(stats["stats"]["monthly_revenue"], D("6500"))
        self.assertEqual(dict(zip(stats["expense_stats"]["chart"]["labels"], stats["expense_stats"]["chart"]["values"])), {"Rent": 20000.0, "Maintenance": 1500.0, "Inventory & supplies": 500.0})
        self.assertEqual(stats["equipment"]["due_count"], 1)
        self.assertEqual(stats["inventory"]["low_count"], 1)
        self.login(self.manager)
        page = self.client.get(reverse("core:home"))
        for text in ("Expenses this month", "Est. operating profit", "Revenue vs expenses", "Equipment status", "Low-stock items"):
            self.assertContains(page, text)

    def test_roles_only_see_what_they_are_allowed_to(self):
        self.login(self.accountant)  # finance, no equipment.manage... but may view equipment and inventory
        page = self.client.get(reverse("core:home"))
        self.assertContains(page, "Expenses this month")
        self.login(self.users["trainer"])
        page = self.client.get(reverse("core:home"))
        for text in ("Expenses this month", "Est. operating profit", "Low-stock items"):
            self.assertNotContains(page, text)
        self.assertContains(page, "Equipment status")  # trainers may view equipment
        stats = get_dashboard_stats(user=self.users["trainer"])
        self.assertNotIn("expense_stats", stats)
        self.assertNotIn("inventory", stats)
        self.login(self.staff)
        self.assertNotContains(self.client.get(reverse("core:home")), "Expenses this month")

    def test_profit_needs_both_finance_permissions(self):
        from apps.accounts.models import RolePermission

        RolePermission.objects.filter(role="accountant", permission="payments.view").delete()
        stats = get_dashboard_stats(user=self.accountant)
        self.assertIn("month", stats["expense_stats"])
        self.assertNotIn("profit", stats["expense_stats"])

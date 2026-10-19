"""Query-efficiency regression tests: the number of SQL queries a page needs must not grow with the number of rows it shows
(that is the signature of an N+1 problem)."""
import os
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.attendance.models import Attendance
from apps.core.demo import seed_demo
from apps.core.models import ActivityLog, Notification
from apps.core.testing import PASSWORD, make_users
from apps.expenses import services as expense_services
from apps.expenses.models import ExpenseCategory
from apps.inventory import services as inventory_services
from apps.members.models import Member
from apps.memberships.models import Membership
from apps.payments import services as payment_services

PAGES = [
    "/", "/members/", "/memberships/", "/memberships/expired/", "/memberships/expiring/", "/payments/", "/payments/outstanding/", "/attendance/",
    "/attendance/history/", "/attendance/report/", "/appointments/?when=all", "/expenses/", "/notifications/", "/audit/", "/inventory/movements/",
    "/trainers/", "/workouts/plans/", "/progress/", "/equipment/", "/reports/payments/", "/reports/outstanding/", "/reports/expenses/",
    "/reports/appointments/", "/reports/membership-status/", "/reports/member-registrations/",
]


class QueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.users = make_users()
        cls.manager = cls.users["manager"]
        cls.demo = seed_demo(cls.manager)

    def count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return len(ctx)

    def grow(self, n):
        today = timezone.localdate()
        monthly = Membership.objects.first().plan
        coach = self.demo["coach"]
        category = ExpenseCategory.objects.get(name="Water")
        for i in range(n):
            m = Member.objects.create(full_name=f"Bulk Member {i}", phone=f"07880{i:05d}", date_of_birth=today - timedelta(days=9000),
                                      emergency_contact_name="K", emergency_contact_phone=f"07881{i:05d}")
            ms = Membership.objects.create(member=m, plan=monthly, start_date=today - timedelta(days=40 + i), end_date=today - timedelta(days=10 + i), price=Decimal("5000"))
            payment_services.record_payment(ms, Decimal("100"), "cash", self.manager, f"bulk{i}")
            Attendance.objects.create(member=m, check_in=timezone.now() - timedelta(minutes=i + 1), check_out=timezone.now() - timedelta(minutes=i))
            appt = Appointment.objects.create(member=m, trainer=coach, start=timezone.now() + timedelta(days=5, hours=i), end=timezone.now() + timedelta(days=5, hours=i + 1), fee=Decimal("3000"))
            payment_services.record_payment(appt, Decimal("50"), "cash", self.manager, f"bulkappt{i}")
            Notification.send(self.manager, f"Bulk {i}", "message")
            ActivityLog.record(self.manager, "bulk", f"bulk entry {i}")
            expense_services.record_expense(category, Decimal("10"), today, f"Bulk expense {i}", self.manager)
            inventory_services.move_stock(self.demo["item"], "purchase", 1, self.manager)

    def test_page_queries_do_not_grow_with_the_number_of_rows(self):
        self.client.login(username="manager", password=PASSWORD)
        before = {url: self.count(url) for url in PAGES}
        self.grow(14)
        after = {url: self.count(url) for url in PAGES}
        if os.environ.get("QUERY_DEBUG"):
            print("QUERIES (before, after):", {u: (before[u], after[u]) for u in PAGES})
        grown = {url: (before[url], after[url]) for url in PAGES if after[url] > before[url] + 1}
        self.assertEqual(grown, {}, "query count grows with row count (N+1)")

    def test_pages_stay_within_a_sane_query_budget(self):
        self.client.login(username="manager", password=PASSWORD)
        self.grow(8)
        heavy = {url: n for url in PAGES if (n := self.count(url)) > 60}
        self.assertEqual(heavy, {})

    def test_member_portal_queries_are_bounded(self):
        self.client.login(username="demo_alice", password="DemoPass-12345!")
        for url in ("/me/", "/me/workouts/", "/me/appointments/", "/me/progress/", "/me/trainers/", "/notifications/"):
            self.assertLess(self.count(url), 40, url)

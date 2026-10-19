"""End-to-end business workflows A-H, driven through the real HTTP views (forms, redirects, permissions), not just services."""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.appointments.models import Appointment
from apps.attendance.models import Attendance
from apps.core.models import Notification
from apps.core.stats import get_dashboard_stats
from apps.core.testing import PASSWORD, future_slot, make_member_with_login, make_users
from apps.expenses.models import Expense, ExpenseCategory
from apps.members.models import Member
from apps.memberships.models import Membership, MembershipPlan
from apps.payments.models import Payment
from apps.trainers.models import Trainer
from apps.workouts.models import Exercise, WorkoutPlan

D = Decimal
TODAY = timezone.localdate
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"


class Flow(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.plan = MembershipPlan.objects.create(name="Monthly", price=D("5000"))

    def login(self, who):
        self.client.logout()
        user = who if isinstance(who, User) else self.users[who]
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD), user.username)

    def form_key(self, url):
        return self.client.get(url).context["form"].fields["idempotency_key"].initial

    def pay(self, membership, amount, method="cash", **extra):
        url = reverse("payments:record", args=[membership.pk])
        return self.client.post(url, {"amount": str(amount), "method": method, "paid_on": TODAY().isoformat(), "idempotency_key": self.form_key(url), **extra})

    def new_member(self, name="Walk-in Wendy", phone="0771230000"):
        data = {"full_name": name, "phone": phone, "email": "", "date_of_birth": "1992-02-02", "address": "", "emergency_contact_name": "Kin",
                "emergency_contact_phone": "0719990000", "registration_date": TODAY().isoformat(), "fitness_goals": ""}
        self.client.post(reverse("members:create"), data)
        return Member.objects.get(phone=phone)

    def assign(self, member, plan=None, start=None):
        self.client.post(reverse("memberships:assign", args=[member.pk]), {"plan": (plan or self.plan).pk, "start_date": (start or TODAY()).isoformat(), "notes": ""})
        return member.memberships.latest("id")


class WorkflowA_Registration(Flow):
    def test_register_assign_pay_receipt_and_status(self):
        self.login("receptionist")
        member = self.new_member()
        self.assertRegex(member.member_id, r"^GM\d{6}$")
        self.assertContains(self.client.get(member.get_absolute_url()), "No membership")
        membership = self.assign(member)
        self.assertEqual((membership.start_date, membership.end_date, membership.price), (TODAY(), self.plan.end_date_for(TODAY()), D("5000")))
        self.assertContains(self.client.get(member.get_absolute_url()), "Active")
        r = self.pay(membership, 5000)
        payment = Payment.objects.get()
        self.assertRedirects(r, payment.get_absolute_url())
        receipt = self.client.get(reverse("payments:receipt", args=[payment.pk]))
        for text in (payment.receipt_number, member.full_name, "Monthly", "LKR 5,000.00", "Outstanding balance"):
            self.assertContains(receipt, text)
        self.assertTrue(self.client.get(reverse("payments:receipt_pdf", args=[payment.pk])).content.startswith(b"%PDF"))
        page = self.client.get(membership.get_absolute_url())
        self.assertEqual((page.context["billing"].payment_status, page.context["billing"].outstanding), ("paid", D("0")))
        self.assertTrue(Notification.objects.filter(kind="membership-started").count() == 0)  # no portal login -> nobody to notify
        self.assertEqual(Member.objects.get(pk=member.pk).memberships.count(), 1)

    def test_cannot_assign_twice_or_pay_for_someone_elses_membership_amount(self):
        self.login("receptionist")
        member = self.new_member()
        m = self.assign(member)
        self.assertContains(self.client.post(reverse("memberships:assign", args=[member.pk]), {"plan": self.plan.pk, "start_date": TODAY().isoformat()}), "already has")
        self.assertContains(self.pay(m, 9999999), "exceeds")
        self.assertEqual(Payment.objects.count(), 0)


class WorkflowB_Attendance(Flow):
    def setUp(self):
        super().setUp()
        self.login("receptionist")
        self.ok = self.new_member("Active Annie", "0771230001")
        self.assign(self.ok)
        self.nobody = self.new_member("No Plan Ned", "0771230002")
        self.expired = self.new_member("Expired Eddie", "0771230003")
        Membership.objects.create(member=self.expired, plan=self.plan, start_date=TODAY() - timedelta(days=60), end_date=TODAY() - timedelta(days=30), price=D("5000"))
        self.suspended = self.new_member("Paused Pat", "0771230004")
        from apps.memberships import services

        services.suspend_membership(self.assign(self.suspended), self.users["manager"], "Injury")

    def test_find_validate_check_in_and_history(self):
        found = self.client.get(reverse("attendance:desk"), {"q": "0771230001"}).context["results"]
        self.assertEqual([m.pk for m in found], [self.ok.pk])
        self.assertEqual(self.client.get(reverse("attendance:desk"), {"q": "annie"}).context["results"][0].membership_status, "active")
        r = self.client.post(reverse("attendance:check_in", args=[self.ok.pk]), follow=True)
        self.assertContains(r, "checked in")
        visit = Attendance.objects.get()
        self.assertEqual((visit.member, visit.method, visit.checked_in_by), (self.ok, "manual", self.users["receptionist"]))
        self.assertContains(self.client.post(reverse("attendance:check_in", args=[self.ok.pk]), follow=True), "Already checked in")
        history = self.client.get(reverse("attendance:history"), {"q": "Annie"}).context["object_list"]
        self.assertEqual([v.pk for v in history], [visit.pk])
        self.assertEqual(self.client.get(reverse("attendance:member_history", args=[self.ok.pk])).context["visit_count"], 1)
        self.client.post(reverse("attendance:check_out", args=[visit.pk]))
        self.assertIsNotNone(Attendance.objects.get().check_out)

    def test_access_is_refused_with_the_reason(self):
        for member, reason in ((self.nobody, "no active membership"), (self.expired, "expired on"), (self.suspended, "suspended")):
            r = self.client.post(reverse("attendance:check_in", args=[member.pk]), follow=True)
            self.assertContains(r, "Check-in refused")
            self.assertContains(r, reason)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_unauthorised_roles_cannot_check_members_in(self):
        for role in ("trainer", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.post(reverse("attendance:check_in", args=[self.ok.pk])).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.post(reverse("attendance:check_in", args=[self.ok.pk])).status_code, 302)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_plan_access_rules_are_enforced_and_qr_scanning_works(self):
        from datetime import time

        weekend_only = MembershipPlan.objects.create(name="Weekend", price=D("1000"), allowed_weekdays="5,6")
        pass_holder = self.new_member("Weekend Wes", "0771230005")
        self.assign(pass_holder, plan=weekend_only)
        today_is_weekend = TODAY().weekday() in (5, 6)
        r = self.client.post(reverse("attendance:check_in", args=[pass_holder.pk]), follow=True)
        self.assertContains(r, "checked in" if today_is_weekend else "only allows access on")
        scan = self.client.post(reverse("attendance:scan"), {"code": f"GYMQR:{self.ok.qr_token}"})
        self.assertContains(scan, "Checked in")
        self.assertContains(self.client.post(reverse("attendance:scan"), {"code": f"GYMQR:{self.ok.qr_token}"}), "Checked out")
        self.assertEqual(self.client.post(reverse("attendance:scan"), {"code": "GYMQR:" + "x" * 43}).status_code, 422)


class WorkflowC_Renewal(Flow):
    def test_renewal_preserves_history_and_updates_dashboard(self):
        self.login("receptionist")
        member = self.new_member("Renewing Rita", "0771230010")
        m = Membership.objects.create(member=member, plan=self.plan, start_date=TODAY() - timedelta(days=25), end_date=TODAY() + timedelta(days=5), price=D("5000"))
        before = get_dashboard_stats()
        self.assertEqual([x.pk for x in before["upcoming_expirations"]], [m.pk])
        r = self.client.post(reverse("memberships:renew", args=[m.pk]), {"plan": self.plan.pk, "notes": ""})
        renewal = Membership.objects.get(previous=m)
        self.assertRedirects(r, renewal.get_absolute_url())
        self.assertEqual(renewal.start_date, m.end_date + timedelta(days=1))  # contiguous, no lost days
        self.assertEqual(renewal.end_date, self.plan.end_date_for(renewal.start_date))
        m.refresh_from_db()
        self.assertEqual((m.start_date, m.end_date, m.status, m.price), (TODAY() - timedelta(days=25), TODAY() + timedelta(days=5), "active", D("5000")))  # untouched
        history = self.client.get(member.get_absolute_url()).context["history"]
        self.assertEqual({h.pk for h in history}, {m.pk, renewal.pk})
        after = get_dashboard_stats()
        self.assertEqual(after["upcoming_expirations"], [])  # already renewed: not nagging about it
        self.assertEqual(after["stats"]["active_members"], before["stats"]["active_members"])
        self.assertContains(self.client.post(reverse("memberships:renew", args=[m.pk]), {"plan": self.plan.pk}), "already been renewed")

    def test_renewing_an_expired_membership_restarts_today_and_moves_the_member_out_of_expired(self):
        self.login("receptionist")
        member = self.new_member("Lapsed Larry", "0771230011")
        old = Membership.objects.create(member=member, plan=self.plan, start_date=TODAY() - timedelta(days=60), end_date=TODAY() - timedelta(days=30), price=D("5000"))
        before = get_dashboard_stats()["stats"]
        self.assertEqual((before["expired_members"], before["active_members"]), (1, 0))
        self.client.post(reverse("memberships:renew", args=[old.pk]), {"plan": self.plan.pk, "notes": ""})
        renewal = Membership.objects.get(previous=old)
        self.assertEqual(renewal.start_date, TODAY())  # no back-dating
        after = get_dashboard_stats()["stats"]
        self.assertEqual((after["expired_members"], after["active_members"]), (0, 1))
        old.refresh_from_db()
        self.assertEqual(old.end_date, TODAY() - timedelta(days=30))


class WorkflowD_PartialPayments(Flow):
    def test_balance_after_each_payment(self):
        self.login("receptionist")
        member = self.new_member("Instalment Ivy", "0771230020")
        m = self.assign(member)
        balance = lambda: self.client.get(m.get_absolute_url()).context["billing"]
        self.assertEqual((balance().outstanding, balance().payment_status), (D("5000"), "unpaid"))
        self.pay(m, "2000.50")
        self.assertEqual((balance().net_paid, balance().outstanding, balance().payment_status), (D("2000.50"), D("2999.50"), "partial"))
        self.assertEqual(get_dashboard_stats()["stats"]["outstanding"], D("2999.50"))
        listed = self.client.get(reverse("payments:outstanding")).context
        self.assertEqual(listed["total_outstanding"], D("2999.50"))
        self.assertContains(self.pay(m, "3000"), "exceeds")  # 0.50 too much
        self.pay(m, "2999.50", method="bank_transfer", external_reference="SLIP-9")
        self.assertEqual((balance().outstanding, balance().payment_status), (D("0"), "paid"))
        self.assertEqual(get_dashboard_stats()["stats"]["outstanding"], D("0"))
        self.assertEqual(self.client.get(reverse("payments:outstanding")).context["object_list"].__len__(), 0)
        self.assertEqual(Payment.objects.count(), 2)

    def test_double_submit_and_refund_authority(self):
        self.login("receptionist")
        m = self.assign(self.new_member("Careful Cleo", "0771230021"))
        url = reverse("payments:record", args=[m.pk])
        key = self.form_key(url)
        data = {"amount": "1000", "method": "cash", "paid_on": TODAY().isoformat(), "idempotency_key": key}
        self.client.post(url, data)
        self.client.post(url, data)
        self.assertEqual(Payment.objects.count(), 1)
        payment = Payment.objects.get()
        self.assertEqual(self.client.get(reverse("payments:refund", args=[payment.pk])).status_code, 403)  # receptionists cannot refund
        self.login("accountant")
        self.assertEqual(self.client.get(reverse("payments:refund", args=[payment.pk])).status_code, 200)


class WorkflowE_PersonalTraining(Flow):
    def test_trainer_availability_booking_conflicts_and_completion(self):
        self.login("manager")
        self.client.post(reverse("trainers:create"), {"full_name": "Coach Casey", "phone": "0771239999", "email": "", "specialties": "Strength", "qualifications": "",
                                                       "bio": "", "employment_status": "active", "hire_date": TODAY().isoformat(), "hourly_rate": "3000"})
        coach = Trainer.objects.get()
        slot = future_slot(3, 10)
        self.client.post(reverse("trainers:availability_add", args=[coach.pk]), {"weekday": slot.weekday(), "start_time": "09:00", "end_time": "17:00"})
        ann, bob = self.new_member_with_plan("Ann", "0771230030"), self.new_member_with_plan("Bob", "0771230031")
        book = lambda member, hour, minute=0: self.client.post(reverse("appointments:book"), {
            "member": member.member_id, "trainer": coach.pk, "date": slot.date().isoformat(), "time": f"{hour:02d}:{minute:02d}", "duration": 60, "notes": ""})
        r = book(ann, 10)
        appt = Appointment.objects.get()
        self.assertRedirects(r, appt.get_absolute_url())
        self.assertEqual((appt.fee, appt.status), (D("3000.00"), "scheduled"))
        self.assertContains(book(bob, 10, 30), "trainer already has")  # overlaps 10:00-11:00
        self.assertContains(book(bob, 8), "not available")  # outside availability
        self.assertContains(self.client.post(reverse("appointments:book"), {  # the same member cannot be in two sessions at once either
            "member": ann.member_id, "trainer": coach.pk, "date": slot.date().isoformat(), "time": "10:15", "duration": 30, "notes": ""}), "already has")
        self.assertEqual(book(bob, 11).status_code, 302)  # back-to-back is fine
        self.assertEqual(Appointment.objects.count(), 2)
        # time passes: the first session has now started and ended
        Appointment.objects.filter(pk=appt.pk).update(start=timezone.now() - timedelta(hours=2), end=timezone.now() - timedelta(hours=1))
        self.assertEqual(get_dashboard_stats()["training"]["completed_sessions"], 0)
        self.client.post(reverse("appointments:complete", args=[appt.pk]))
        appt.refresh_from_db()
        self.assertEqual(appt.status, "completed")
        self.assertEqual(get_dashboard_stats()["training"]["completed_sessions"], 1)
        self.assertEqual(self.client.post(reverse("appointments:cancel", args=[appt.pk]), {"reason": "oops"}).status_code, 200)  # outcome is final

    def new_member_with_plan(self, name, phone):
        self.login("receptionist")
        m = self.new_member(name, phone)
        self.assign(m)
        self.login("manager")
        return m


class WorkflowF_WorkoutPlans(Flow):
    def test_trainer_builds_plan_and_member_can_only_view_and_log_their_own(self):
        member, member_login = make_member_with_login(self.users, "Plan Paula")
        other, other_login = make_member_with_login(self.users, "Other Olly")
        coach = Trainer.objects.create(full_name="Coach", phone="0771111111", user=self.users["trainer"])
        from apps.trainers import services as trainer_services

        trainer_services.assign_trainer(coach, member, self.users["manager"])
        self.login("trainer")
        self.client.post(reverse("workouts:exercise_create"), {"name": "Goblet Squat", "muscle_group": "legs", "category": "strength", "description": "",
                                                              "instructions": "Hold the weight at your chest.", "video_url": "", "is_active": "on"})
        exercise = Exercise.objects.get(name="Goblet Squat")
        self.client.post(reverse("workouts:plan_create"), {"name": "Leg day", "member": member.member_id, "goal": "Strength", "description": "",
                                                           "start_date": TODAY().isoformat(), "end_date": ""})
        plan = WorkoutPlan.objects.get()
        self.assertEqual(plan.member, member)
        self.client.post(reverse("workouts:item_add", args=[plan.pk]), {"exercise": exercise.pk, "weekday": 0, "sets": 4, "reps": 8, "weight_kg": "20", "rest_seconds": 60, "notes": ""})
        item = plan.items.get()
        # the member sees the plan in the portal ...
        self.login(member_login)
        page = self.client.get(reverse("portal:workout", args=[plan.pk]))
        self.assertContains(page, "Goblet Squat")
        self.assertContains(page, "4 × 8")
        self.assertContains(self.client.get(reverse("portal:workouts")), "Leg day")
        # ... but cannot change it through any staff URL
        for method, name, args in (("get", "workouts:plan_edit", [plan.pk]), ("post", "workouts:plan_status", [plan.pk]), ("post", "workouts:item_remove", [item.pk]),
                                   ("get", "workouts:item_edit", [item.pk]), ("get", "workouts:item_add", [plan.pk]), ("get", "workouts:exercise_create", []),
                                   ("get", "workouts:plan_copy", [plan.pk]), ("get", "workouts:plan_log", [plan.pk])):
            self.assertEqual(getattr(self.client, method)(reverse(name, args=args), {"status": "archived"} if method == "post" else {}).status_code, 403, name)
        plan.refresh_from_db()
        self.assertEqual((plan.status, plan.items.count(), plan.name), ("active", 1, "Leg day"))
        # another member can neither see nor log against it
        self.login(other_login)
        self.assertEqual(self.client.get(reverse("portal:workout", args=[plan.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("portal:workout_log", args=[plan.pk]), {}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("portal:workouts")), "Leg day")
        # the owner can record their own workout (and nothing else changes)
        self.login(member_login)
        done = self.client.post(reverse("portal:workout_log", args=[plan.pk]), {"performed_on": TODAY().isoformat(), "notes": "", f"done_{item.pk}": "on",
                                                                                f"sets_{item.pk}": 4, f"reps_{item.pk}": 8, f"weight_{item.pk}": "20"})
        self.assertEqual(done.status_code, 302)
        item.refresh_from_db()
        self.assertEqual((item.sets, item.reps), (4, 8))


class WorkflowG_ExpensesAndReports(Flow):
    def test_financial_reports_and_dashboard_use_the_same_figures(self):
        tmp = tempfile.mkdtemp()
        try:
            with override_settings(PRIVATE_MEDIA_ROOT=tmp):
                self.login("manager")
                r = self.client.post(reverse("expenses:create"), {"category": ExpenseCategory.objects.get(name="Rent").pk, "amount": "12000.00", "expense_date": TODAY().isoformat(), "description": "October rent",
                                                                  "supplier": "", "reference": "R-1", "receipt": SimpleUploadedFile("r.pdf", PDF)})
                self.assertEqual(r.status_code, 302)
                expense = Expense.objects.get()
                self.assertEqual((expense.amount, expense.entered_by, bool(expense.receipt)), (D("12000.00"), self.users["manager"], True))
                self.login("receptionist")
                m = self.assign(self.new_member("Report Rhea", "0771230040"))
                self.pay(m, 3000)
                self.pay(m, 1000, method="bank_transfer", external_reference="S-1")
                self.login("manager")
                params = {"from": TODAY().replace(day=1).isoformat(), "to": TODAY().isoformat()}
                summary = dict((l, v) for l, v, _ in self.client.get(reverse("reports:report", args=["revenue-vs-expense"]), params).context["summary"])
                self.assertEqual(summary, {"Revenue collected": D("4000"), "Expenses": D("12000"), "Estimated operating profit": D("-8000")})
                revenue = dict((l, v) for l, v, _ in self.client.get(reverse("reports:report", args=["revenue"]), params).context["summary"])
                self.assertEqual((revenue["Collected (received)"], revenue["Revenue (net collected)"], revenue["Outstanding now (receivable)"], revenue["Invoiced in period"]),
                                 (D("4000"), D("4000"), D("1000"), D("5000")))
                stats = get_dashboard_stats(user=self.users["manager"])
                self.assertEqual((stats["stats"]["monthly_revenue"], stats["stats"]["todays_revenue"], stats["stats"]["outstanding"]), (D("4000"), D("4000"), D("1000")))
                self.assertEqual((stats["expense_stats"]["month"], stats["expense_stats"]["profit"]), (D("12000"), D("-8000")))
                page = self.client.get(reverse("core:home"))
                self.assertContains(page, "LKR 4,000.00")
                self.assertContains(page, "LKR 12,000.00")
                # a voided expense disappears from every figure
                self.client.post(reverse("expenses:void", args=[expense.pk]), {"reason": "Entered twice"})
                summary = dict((l, v) for l, v, _ in self.client.get(reverse("reports:report", args=["revenue-vs-expense"]), params).context["summary"])
                self.assertEqual((summary["Expenses"], summary["Estimated operating profit"]), (D("0"), D("4000")))
                self.assertEqual(get_dashboard_stats(user=self.users["manager"])["expense_stats"]["month"], D("0"))
                # a refund lowers revenue by exactly its amount
                payment = Payment.objects.filter(kind="payment", method="cash").get()
                self.client.post(reverse("payments:refund", args=[payment.pk]), {"amount": "500", "method": "cash", "reason": "Goodwill", "idempotency_key": "refundg1"})
                self.assertEqual(get_dashboard_stats()["stats"]["monthly_revenue"], D("3500"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WorkflowH_Notifications(Flow):
    def test_expiry_reminder_reaches_the_right_inbox_once(self):
        member, login = make_member_with_login(self.users, "Expiring Ellie", membership=False)
        bystander, bystander_login = make_member_with_login(self.users, "Bystander Ben", membership=False)
        m = Membership.objects.create(member=member, plan=self.plan, start_date=TODAY() - timedelta(days=25), end_date=TODAY() + timedelta(days=5), price=D("5000"))
        call_command("run_scheduled_notifications", stdout=StringIO())
        mine = Notification.objects.filter(user=login, kind="membership-expiring")
        self.assertEqual(mine.count(), 1)
        self.assertEqual(mine.get().dedupe_key, f"membership-expiring:{m.pk}:7")
        # appears in the member's own notification centre, with the unread badge ...
        self.login(login)
        page = self.client.get(reverse("core:notifications"))
        self.assertContains(page, "Membership expiring soon")
        self.assertContains(page, "ends in 5 days")
        # (an unpaid membership started 25 days ago is legitimately also "payment overdue")
        self.assertEqual(set(Notification.objects.filter(user=login).values_list("kind", flat=True)), {"membership-expiring", "payment-due"})
        self.assertEqual(page.context["unread_notifications"], 2)
        # ... and in staff's digest, but not in anyone else's inbox
        self.login(bystander_login)
        self.assertNotContains(self.client.get(reverse("core:notifications")), "Expiring")
        self.assertFalse(Notification.objects.filter(user=bystander_login, kind="membership-expiring").exists())
        self.login("manager")
        self.assertContains(self.client.get(reverse("core:notifications")), "Memberships expiring soon")
        # running again (cron + Celery overlap, retries, restarts) never duplicates
        total = Notification.objects.count()
        call_command("run_scheduled_notifications", stdout=StringIO())
        call_command("run_scheduled_notifications", stdout=StringIO())
        self.assertEqual(Notification.objects.count(), total)
        # reading it marks only that one as read
        note = mine.get()
        self.login(login)
        self.client.post(reverse("core:notification_read", args=[note.pk]))
        note.refresh_from_db()
        self.assertIsNotNone(note.read_at)
        self.assertEqual(self.client.get(reverse("core:notifications")).context["unread_notifications"], 1)  # only the one that was opened

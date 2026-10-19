from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.appointments import services as appointment_services
from apps.appointments.models import Appointment
from apps.core.models import Notification
from apps.core.stats import get_dashboard_stats
from apps.core.testing import PASSWORD, future_slot, give_membership, make_member_with_login, make_trainer, make_users
from apps.members.tests import make_member
from apps.progress.models import MeasurementType, ProgressRecord
from apps.trainers import services as trainer_services
from apps.workouts.models import Exercise, PlanExercise, WorkoutPlan, WorkoutSession


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.manager = self.users["manager"]
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"], rate="3000")
        self.member, self.login_a = make_member_with_login(self.users, "Alice Member")
        self.other, self.login_b = make_member_with_login(self.users, "Bob Other")
        trainer_services.assign_trainer(self.trainer, self.member, self.manager)
        self.squat = Exercise.objects.create(name="Squat", muscle_group="legs")

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def plan_for(self, member, name="Plan"):
        plan = WorkoutPlan.objects.create(member=member, trainer=self.trainer, name=name)
        PlanExercise.objects.create(plan=plan, exercise=self.squat, weekday=0, sets=3, reps=10)
        return plan


class AccessTests(Base):
    def test_portal_is_for_linked_members_only(self):
        pages = [reverse(n) for n in ("portal:home", "portal:trainers", "portal:workouts", "portal:appointments", "portal:book", "portal:progress")]
        self.client.logout()
        for url in pages:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        for role in ("super_admin", "manager", "receptionist", "trainer", "accountant"):
            self.login(self.users[role])
            for url in pages:
                self.assertEqual(self.client.get(url).status_code, 403, (role, url))
        self.login(self.login_a)
        for url in pages:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_unlinked_member_login_gets_a_friendly_page(self):
        self.login(self.users["member"])
        r = self.client.get(reverse("portal:home"))
        self.assertContains(r, "not linked")
        self.assertContains(self.client.get(reverse("portal:workouts")), "not linked")

    def test_home_redirects_members_and_shows_their_data(self):
        self.login(self.login_a)
        self.assertRedirects(self.client.get(reverse("core:home")), reverse("portal:home"), fetch_redirect_response=False)
        plan = self.plan_for(self.member, "Alice plan")
        a = appointment_services.book(self.member, self.trainer, future_slot(3), 60, self.manager)
        r = self.client.get(reverse("portal:home"))
        self.assertContains(r, "Alice Member")
        self.assertContains(r, "Coach Kim")
        self.assertContains(r, "Alice plan")
        self.assertContains(r, "Monthly")  # membership card
        self.assertNotContains(r, "Bob Other")

    def test_members_cannot_reach_staff_pages(self):
        self.login(self.login_a)
        for name, args in (("members:list", []), ("members:detail", [self.other.pk]), ("trainers:list", []), ("workouts:plan_list", []),
                           ("appointments:list", []), ("progress:member", [self.member.pk]), ("payments:list", []), ("attendance:desk", [])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)


class OwnershipTests(Base):
    def test_workout_plans_are_private_to_their_member(self):
        mine, theirs = self.plan_for(self.member, "Mine"), self.plan_for(self.other, "Theirs")
        self.login(self.login_a)
        r = self.client.get(reverse("portal:workouts"))
        self.assertContains(r, "Mine")
        self.assertNotContains(r, "Theirs")
        self.assertEqual(self.client.get(reverse("portal:workout", args=[mine.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("portal:workout", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("portal:workout_log", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("portal:workout_log", args=[theirs.pk]), {}).status_code, 404)

    def test_member_records_own_workout(self):
        plan = self.plan_for(self.member)
        item = plan.items.get()
        self.login(self.login_a)
        data = {"performed_on": timezone.localdate().isoformat(), "notes": "", f"done_{item.pk}": "on", f"sets_{item.pk}": 3, f"reps_{item.pk}": 9, f"weight_{item.pk}": "", f"duration_{item.pk}": ""}
        r = self.client.post(reverse("portal:workout_log", args=[plan.pk]), data)
        self.assertRedirects(r, reverse("portal:workout", args=[plan.pk]))
        s = WorkoutSession.objects.get()
        self.assertEqual((s.member, s.recorded_by, s.entries.get().reps_done), (self.member, self.login_a, 9))
        self.assertContains(self.client.get(reverse("portal:workout", args=[plan.pk])), "Recorded workouts")

    def test_cannot_record_on_a_finished_plan(self):
        plan = self.plan_for(self.member)
        plan.status = "completed"
        plan.save()
        item = plan.items.get()
        self.login(self.login_a)
        self.client.post(reverse("portal:workout_log", args=[plan.pk]), {"performed_on": timezone.localdate().isoformat(), f"done_{item.pk}": "on", f"sets_{item.pk}": 3, f"reps_{item.pk}": 9})
        self.assertFalse(WorkoutSession.objects.exists())

    def test_progress_is_own_only_and_member_source_is_recorded(self):
        ProgressRecord.objects.create(member=self.other, weight_kg=Decimal("99.9"), notes="secret bob note")
        waist = MeasurementType.objects.get(name="Waist")
        self.login(self.login_a)
        r = self.client.post(reverse("portal:progress"), {"recorded_on": timezone.localdate().isoformat(), "weight_kg": "68.2", "height_cm": "170", "notes": "Start", f"m_{waist.pk}": "75"})
        self.assertRedirects(r, reverse("portal:progress"))
        rec = ProgressRecord.objects.get(member=self.member)
        self.assertEqual((rec.source, rec.recorded_by, rec.measurements.get().value), ("member", self.login_a, Decimal("75.0")))
        page = self.client.get(reverse("portal:progress"))
        self.assertContains(page, "Start")
        self.assertNotContains(page, "99.9")
        self.assertNotContains(page, "secret bob note")
        self.assertEqual(self.client.post(reverse("portal:progress"), {"recorded_on": timezone.localdate().isoformat(), "weight_kg": "1"}).status_code, 200)
        self.assertEqual(ProgressRecord.objects.filter(member=self.member).count(), 1)
        # trainers see it because Alice is assigned to them; Bob's is invisible to them
        self.login(self.users["trainer"])
        self.assertEqual(self.client.get(reverse("progress:member", args=[self.member.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("progress:member", args=[self.other.pk])).status_code, 404)

    def test_trainers_page_lists_only_assigned_trainers(self):
        make_trainer("Unassigned Coach")
        self.login(self.login_a)
        r = self.client.get(reverse("portal:trainers"))
        self.assertContains(r, "Coach Kim")
        self.assertNotContains(r, "Unassigned Coach")
        self.login(self.login_b)
        self.assertNotContains(self.client.get(reverse("portal:trainers")), "Coach Kim")


class PortalBookingTests(Base):
    def payload(self, **kw):
        slot = future_slot(3, 10)
        d = {"trainer": self.trainer.pk, "date": slot.date().isoformat(), "time": "10:00", "duration": 60, "notes": ""}
        d.update(kw)
        return d

    def test_member_books_reschedules_and_cancels(self):
        self.login(self.login_a)
        r = self.client.post(reverse("portal:book"), self.payload())
        self.assertRedirects(r, reverse("portal:appointments"))
        a = Appointment.objects.get()
        self.assertEqual((a.member, a.booked_via, a.fee), (self.member, "portal", Decimal("3000.00")))
        self.assertTrue(Notification.objects.filter(user=self.users["trainer"], title="Session booked").exists())
        r = self.client.post(reverse("portal:reschedule", args=[a.pk]), {"date": future_slot(4).date().isoformat(), "time": "11:00"})
        self.assertRedirects(r, reverse("portal:appointments"))
        a.refresh_from_db()
        self.assertEqual(timezone.localtime(a.start).hour, 11)
        self.assertContains(self.client.get(reverse("portal:appointments")), "Reschedule")
        r = self.client.post(reverse("portal:cancel", args=[a.pk]), {"reason": "Busy at work"})
        self.assertRedirects(r, reverse("portal:appointments"))
        a.refresh_from_db()
        self.assertEqual((a.status, a.cancelled_by), ("cancelled", self.login_a))

    def test_member_conflicts_are_reported(self):
        appointment_services.book(self.other, self.trainer, future_slot(3, 10), 60, self.manager)
        self.login(self.login_a)
        r = self.client.post(reverse("portal:book"), self.payload())
        self.assertContains(r, "trainer already has")
        self.assertEqual(Appointment.objects.count(), 1)
        r = self.client.post(reverse("portal:book"), self.payload(time="05:00"))
        self.assertContains(r, "not available")

    def test_member_can_only_book_assigned_trainers(self):
        stranger = make_trainer("Stranger Coach")
        self.login(self.login_a)
        r = self.client.post(reverse("portal:book"), self.payload(trainer=stranger.pk))
        self.assertEqual(r.status_code, 200)  # form error: not a valid choice
        self.assertFalse(Appointment.objects.exists())
        slot_day = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.assertEqual(self.client.get(reverse("portal:slots"), {"trainer": stranger.pk, "date": slot_day, "duration": 60}).json(), {"slots": []})
        self.assertTrue(self.client.get(reverse("portal:slots"), {"trainer": self.trainer.pk, "date": slot_day, "duration": 60}).json()["slots"])

    def test_other_members_sessions_are_not_reachable(self):
        theirs = appointment_services.book(self.other, self.trainer, future_slot(5), 60, self.manager)
        self.login(self.login_a)
        for name in ("portal:reschedule", "portal:cancel"):
            self.assertEqual(self.client.get(reverse(name, args=[theirs.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse(name, args=[theirs.pk]), {"reason": "xxx", "date": "2030-01-01", "time": "10:00"}).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "scheduled")
        self.assertNotContains(self.client.get(reverse("portal:appointments")), "Bob")

    def test_short_notice_changes_are_blocked_for_members(self):
        near = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() + timedelta(hours=4), end=timezone.now() + timedelta(hours=5))
        self.login(self.login_a)
        r = self.client.post(reverse("portal:cancel", args=[near.pk]), {"reason": "Cannot come"})
        self.assertContains(r, "hours notice")
        self.assertContains(self.client.get(reverse("portal:appointments")), "call the gym")
        near.refresh_from_db()
        self.assertEqual(near.status, "scheduled")


class NotificationTests(Base):
    def test_read_state_and_isolation(self):
        Notification.objects.all().delete()  # ignore the assignment notifications created in setUp
        n1 = Notification.send(self.login_a, "Hello A", "msg")
        n2 = Notification.send(self.login_b, "Hello B", "secret")
        self.login(self.login_a)
        r = self.client.get(reverse("core:notifications"))
        self.assertContains(r, "Hello A")
        self.assertNotContains(r, "Hello B")
        self.assertEqual(r.context["unread_notifications"], 1)
        self.assertEqual(self.client.post(reverse("core:notification_read", args=[n2.pk])).status_code, 404)  # not yours
        n2.refresh_from_db()
        self.assertIsNone(n2.read_at)
        self.client.post(reverse("core:notification_read", args=[n1.pk]), {"next": "https://evil.example/"})
        n1.refresh_from_db()
        self.assertIsNotNone(n1.read_at)
        self.assertEqual(self.client.get(reverse("core:notifications")).context["unread_notifications"], 0)

    def test_mark_all_read_and_open_redirect_protection(self):
        for i in range(3):
            Notification.send(self.login_a, f"N{i}", "m")
        Notification.send(self.login_b, "B", "m")
        self.login(self.login_a)
        r = self.client.post(reverse("core:notifications_read_all"))
        self.assertRedirects(r, reverse("core:notifications"))
        self.assertFalse(Notification.objects.filter(user=self.login_a, read_at__isnull=True).exists())
        self.assertTrue(Notification.objects.filter(user=self.login_b, read_at__isnull=True).exists())
        n = Notification.send(self.login_a, "Link", "m", "/me/appointments/")
        r = self.client.post(reverse("core:notification_read", args=[n.pk]), {"next": "/me/appointments/"})
        self.assertRedirects(r, "/me/appointments/", fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("core:notifications_read_all")).status_code, 405)

    def test_inactive_users_receive_nothing_and_anonymous_is_redirected(self):
        self.login_b.is_active = False
        self.login_b.save()
        self.assertIsNone(Notification.send(self.login_b, "x", "y"))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core:notifications")).status_code, 302)


class PortalAccessManagementTests(Base):
    def setUp(self):
        super().setUp()
        self.plain = make_member(full_name="No Login Yet")
        self.plain.email = "nologin@example.com"
        self.plain.save()
        self.url = reverse("members:portal_access", args=[self.plain.pk])

    def login_as(self, role):
        self.login(self.users[role])

    def test_create_login_emails_a_password_link(self):
        self.login_as("manager")
        r = self.client.post(self.url, {"action": "create"})
        self.assertRedirects(r, self.plain.get_absolute_url())
        self.plain.refresh_from_db()
        user = self.plain.user
        self.assertEqual((user.role, user.email, user.username), (Role.MEMBER, "nologin@example.com", self.plain.member_id.lower()))
        self.assertTrue(user.has_usable_password())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/password/reset/", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["nologin@example.com"])
        r = self.client.post(self.url, {"action": "create"}, follow=True)
        self.assertContains(r, "already has")

    def test_create_requires_an_email_and_unique_address(self):
        self.login_as("manager")
        self.plain.email = ""
        self.plain.save()
        self.assertContains(self.client.post(self.url, {"action": "create"}, follow=True), "Add an email")
        self.plain.email = self.login_a.email
        self.plain.save()
        self.assertContains(self.client.post(self.url, {"action": "create"}, follow=True), "already exists")
        self.assertIsNone(type(self.plain).objects.get(pk=self.plain.pk).user)

    def test_link_and_unlink_existing_account(self):
        spare = User.objects.create_user(username="spare", email="spare@example.com", password=PASSWORD, role=Role.MEMBER)
        self.login_as("manager")
        self.client.post(self.url, {"action": "link", "user": spare.pk})
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.user, spare)
        self.client.post(self.url, {"action": "link", "user": self.users["manager"].pk})  # staff accounts cannot be linked
        self.assertEqual(self.client.post(reverse("members:portal_access", args=[self.other.pk]), {"action": "link", "user": spare.pk}, follow=True).status_code, 200)
        self.other.refresh_from_db()
        self.assertEqual(self.other.user, self.login_b)  # unchanged
        self.client.post(self.url, {"action": "unlink"})
        self.plain.refresh_from_db()
        self.assertIsNone(self.plain.user)
        self.assertTrue(User.objects.filter(pk=spare.pk).exists())

    def test_requires_users_manage_permission(self):
        for role in ("receptionist", "trainer", "accountant", "member"):
            self.login_as(role)
            self.assertEqual(self.client.post(self.url, {"action": "create"}).status_code, 403, role)
        self.plain.refresh_from_db()
        self.assertIsNone(self.plain.user)


class DashboardRoleTests(Base):
    def test_trainer_sees_training_stats_but_no_finance(self):
        appointment_services.book(self.member, self.trainer, future_slot(3), 60, self.manager)
        other_trainer = make_trainer("Other Coach")
        trainer_services.assign_trainer(other_trainer, self.other, self.manager)
        appointment_services.book(self.other, other_trainer, future_slot(4), 60, self.manager)
        self.plan_for(self.member)
        self.plan_for(self.other)
        self.login(self.users["trainer"])
        r = self.client.get(reverse("core:home"))
        self.assertEqual(r.status_code, 200)
        training = r.context["training"]
        self.assertEqual((training["upcoming_appointments"], training["plans_assigned"], training["total_trainers"]), (1, 1, 2))
        for text in ("Upcoming sessions", "Completed sessions", "Active workout plans"):
            self.assertContains(r, text)
        for text in ("Revenue collected", "Outstanding (unpaid fees)", "Recent payments", "Expired memberships", "Total members",
                     "Upcoming membership expirations", "Active accounts by role"):
            self.assertNotContains(r, text)

    def test_manager_sees_everything_and_accountant_sees_finance_only(self):
        appointment_services.book(self.member, self.trainer, future_slot(3), 60, self.manager)
        self.login(self.manager)
        r = self.client.get(reverse("core:home"))
        self.assertEqual(r.context["training"]["upcoming_appointments"], 1)
        for text in ("Revenue collected today", "Upcoming sessions", "Active trainers", "Total members"):
            self.assertContains(r, text)
        self.login(self.users["accountant"])
        r = self.client.get(reverse("core:home"))
        self.assertContains(r, "Revenue collected today")
        self.assertNotContains(r, "Upcoming sessions")
        self.assertNotContains(r, "Active workout plans")

    def test_receptionist_sees_sessions_but_not_workout_plans(self):
        self.login(self.users["receptionist"])
        r = self.client.get(reverse("core:home"))
        self.assertContains(r, "Upcoming sessions")
        self.assertContains(r, "Total members")
        self.assertNotContains(r, "Active workout plans")  # no workouts permission
        self.assertNotContains(r, "Active accounts by role")  # no users permission

    def test_completed_sessions_counted(self):
        done = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() - timedelta(days=1),
                                          end=timezone.now() - timedelta(days=1) + timedelta(hours=1), status="completed")
        self.assertEqual(get_dashboard_stats()["training"]["completed_sessions"], 1)
        self.assertEqual(get_dashboard_stats(user=self.users["trainer"])["training"]["completed_sessions"], 1)
        self.assertEqual(get_dashboard_stats(user=make_trainer("Another", user=User.objects.create_user(username="t2", email="t2@example.com", password=PASSWORD, role="trainer")).user)["training"]["completed_sessions"], 0)


class PortalPaymentsTests(Base):
    def setUp(self):
        super().setUp()
        from apps.payments import services as pay_services

        self.alice_ms = self.member.memberships.get()
        self.bob_ms = self.other.memberships.get()
        self.alice_pay, _ = pay_services.record_payment(self.alice_ms, Decimal("2000"), "cash", self.manager, "pa1")
        self.bob_pay, _ = pay_services.record_payment(self.bob_ms, Decimal("1234"), "cash", self.manager, "pb1")
        self.adjustment, _ = pay_services.adjust_charge(self.alice_ms, Decimal("-100"), self.manager, "pa2", "Promo")

    def test_member_sees_only_their_own_balance_history_and_receipts(self):
        self.login(self.login_a)
        page = self.client.get(reverse("portal:payments"))
        self.assertContains(page, self.alice_pay.receipt_number)
        self.assertNotContains(page, self.bob_pay.receipt_number)
        self.assertNotContains(page, "1,234.00")
        self.assertNotContains(page, self.adjustment.receipt_number)  # adjustments are internal, not receipts
        self.assertEqual(page.context["owed"], Decimal("2900"))  # 5000 - 100 discount - 2000 paid
        receipt = self.client.get(reverse("portal:receipt", args=[self.alice_pay.pk]))
        self.assertContains(receipt, "Outstanding balance")
        self.assertContains(receipt, reverse("portal:payments"))  # back link stays inside the portal
        self.assertNotContains(receipt, 'href="%s"' % reverse("payments:detail", args=[self.alice_pay.pk]))
        self.assertTrue(self.client.get(reverse("portal:receipt_pdf", args=[self.alice_pay.pk])).content.startswith(b"%PDF"))

    def test_other_members_receipts_and_staff_receipts_are_unreachable(self):
        self.login(self.login_a)
        for name in ("portal:receipt", "portal:receipt_pdf"):
            self.assertEqual(self.client.get(reverse(name, args=[self.bob_pay.pk])).status_code, 404, name)
            self.assertEqual(self.client.get(reverse(name, args=[self.adjustment.pk])).status_code, 404, name)
        self.assertEqual(self.client.get(reverse("payments:receipt", args=[self.alice_pay.pk])).status_code, 403)  # staff URL stays staff-only
        self.assertEqual(self.client.get(reverse("payments:list")).status_code, 403)
        self.login(self.users["manager"])
        self.assertEqual(self.client.get(reverse("portal:payments")).status_code, 403)  # staff do not use the portal

    def test_pending_and_voided_entries(self):
        from apps.payments import services as pay_services

        pending, _ = pay_services.record_payment(self.alice_ms, Decimal("500"), "bank_transfer", self.manager, "pa3", pending=True)
        voided, _ = pay_services.record_payment(self.alice_ms, Decimal("100"), "cash", self.manager, "pa4")
        pay_services.void_payment(voided, self.manager, "Wrong member")
        self.login(self.login_a)
        page = self.client.get(reverse("portal:payments"))
        self.assertContains(page, pending.receipt_number)
        self.assertContains(page, "Pending confirmation")
        self.assertNotContains(page, reverse("portal:receipt", args=[voided.pk]))  # (its number may still appear in the notification bell)
        self.assertContains(self.client.get(reverse("portal:receipt", args=[pending.pk])), "NOT A VALID RECEIPT")

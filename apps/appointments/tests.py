from datetime import date, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Notification
from apps.notifications.models import NotificationDelivery
from apps.core.stats import get_dashboard_stats
from apps.core.testing import PASSWORD, future_slot, give_membership, make_member_with_login, make_trainer, make_users
from apps.members.tests import make_member
from apps.payments import services as pay
from apps.payments.billing import appointment_billing
from apps.payments.models import Payment
from apps.trainers import services as trainer_services
from apps.trainers.models import TrainerAvailability, TrainerTimeOff

from . import services
from .models import Appointment, Status
from .services import BookingError


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.staff = self.users["receptionist"]
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"], rate="3000")
        self.member, self.member_login = make_member_with_login(self.users, "Alice Member")
        trainer_services.assign_trainer(self.trainer, self.member, self.users["manager"])

    def login(self, role=None, user=None):
        self.client.logout()
        user = user or self.users[role]
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def book(self, days=3, hour=10, minute=0, duration=60, member=None, trainer=None, **kw):
        return services.book(member or self.member, trainer or self.trainer, future_slot(days, hour, minute), duration, kw.pop("user", self.staff), **kw)


class BookingTests(Base):
    def test_successful_booking_snapshots_fee_and_notifies(self):
        a = self.book(duration=90)
        self.assertEqual((a.status, a.duration_minutes, a.fee, a.booked_via), ("scheduled", 90, Decimal("4500.00"), "staff"))
        self.assertEqual(a.events.get().action, "booked")
        self.trainer.hourly_rate = Decimal("9999")
        self.trainer.save()
        a.refresh_from_db()
        self.assertEqual(a.fee, Decimal("4500.00"))  # later rate changes never alter booked sessions
        self.assertEqual(Notification.objects.filter(user=self.users["trainer"], title="Session booked").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.member_login, title="Session booked").count(), 1)
        self.assertFalse(Notification.objects.filter(user=self.staff).exists())  # not the person who booked

    def test_free_sessions(self):
        free = make_trainer("Free Coach", rate="0")
        trainer_services.assign_trainer(free, self.member, self.users["manager"])
        self.assertEqual(self.book(trainer=free).fee, Decimal("0.00"))

    def test_overlap_rules_for_the_same_trainer(self):
        self.book(hour=10)  # 10:00-11:00
        other = make_member_with_login(self.users, "Bob")[0]
        for hour, minute, duration in ((10, 0, 60), (10, 30, 60), (9, 30, 60), (10, 15, 30), (9, 0, 90)):
            with self.assertRaisesMessage(BookingError, "trainer already has"):
                self.book(hour=hour, minute=minute, duration=duration, member=other)
        self.book(hour=11, member=other)  # back-to-back is fine
        self.book(hour=9, member=make_member_with_login(self.users, "Cy")[0])  # ends exactly at 10:00
        self.assertEqual(Appointment.objects.count(), 3)

    def test_cancelled_sessions_free_the_slot(self):
        a = self.book(hour=10)
        services.cancel(a, self.staff, "Illness")
        self.book(hour=10, member=make_member_with_login(self.users, "Bob")[0])

    def test_member_cannot_be_double_booked_with_another_trainer(self):
        second = make_trainer("Second Coach")
        self.book(hour=10)
        with self.assertRaisesMessage(BookingError, "member already has"):
            self.book(hour=10, minute=30, trainer=second)

    def test_database_blocks_duplicate_slot_even_if_application_checks_are_bypassed(self):
        a = self.book(hour=10)
        other = make_member(full_name="Other")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Appointment.objects.create(member=other, trainer=self.trainer, start=a.start, end=a.end)

    def test_losing_a_booking_race_is_reported_cleanly(self):
        slot = future_slot(3, 10)
        other = make_member_with_login(self.users, "Bob")[0]
        with mock.patch.object(services, "validate_slot", side_effect=lambda *a, **k: slot + timedelta(minutes=60)):
            self.book(hour=10)  # the "other" request wins the slot
            with self.assertRaisesMessage(BookingError, "just taken"):
                self.book(hour=10, member=other)  # our checks passed (patched) but the unique index caught it
        self.assertEqual(Appointment.objects.count(), 1)

    def test_validation_rules(self):
        cases = [
            (dict(duration=20), "session lengths"),
            (dict(minute=10), "15-minute mark"),
            (dict(hour=5), "not available"),          # before 06:00
            (dict(hour=19, minute=30, duration=60), "not available"),  # ends 20:30
            (dict(days=-1), "future"),
            (dict(days=200), "at most"),
        ]
        for kwargs, text in cases:
            with self.assertRaisesMessage(BookingError, text):
                self.book(**kwargs)
        self.assertEqual(Appointment.objects.count(), 0)

    def test_session_cannot_cross_midnight(self):
        TrainerAvailability.objects.filter(trainer=self.trainer).delete()
        TrainerAvailability.objects.create(trainer=self.trainer, weekday=future_slot(3).weekday(), start_time=time(0), end_time=time(23, 59))
        with self.assertRaisesMessage(BookingError, "midnight"):
            self.book(hour=23, minute=45, duration=30)

    def test_time_off_and_inactive_trainer(self):
        day = timezone.localdate() + timedelta(days=3)
        TrainerTimeOff.objects.create(trainer=self.trainer, start_date=day, end_date=day, reason="Leave")
        with self.assertRaisesMessage(BookingError, "on leave"):
            self.book()
        self.trainer.employment_status = "on_leave"
        self.trainer.save()
        with self.assertRaisesMessage(BookingError, "not taking bookings"):
            self.book(days=5)

    def test_membership_requirement(self):
        nobody = make_member(full_name="No Membership")
        with self.assertRaisesMessage(BookingError, "membership"):
            self.book(member=nobody)
        with override_settings(APPOINTMENT_REQUIRE_MEMBERSHIP=False):
            self.book(member=nobody)

    def test_deactivated_member(self):
        self.member.is_active = False
        self.member.save()
        with self.assertRaisesMessage(BookingError, "deactivated"):
            self.book()

    def test_member_self_booking_rules(self):
        stranger = make_trainer("Not Assigned")
        with self.assertRaisesMessage(BookingError, "assigned trainer"):
            services.book(self.member, stranger, future_slot(3), 60, self.member_login, as_member=True)
        soon = timezone.now() + timedelta(minutes=30)
        soon = soon.replace(minute=(soon.minute // 15) * 15 + 15 if soon.minute < 45 else 0, second=0, microsecond=0)
        with self.assertRaises(BookingError):  # too soon for a member (and maybe outside hours): either way refused
            services.book(self.member, self.trainer, soon, 60, self.member_login, as_member=True)
        a = services.book(self.member, self.trainer, future_slot(3), 60, self.member_login, as_member=True)
        self.assertEqual(a.booked_via, "portal")
        self.assertTrue(Notification.objects.filter(user=self.users["trainer"]).exists())
        self.assertFalse(Notification.objects.filter(user=self.member_login, title="Session booked").exists())


class RescheduleCancelTests(Base):
    def test_reschedule_keeps_payment_and_rearms_reminder(self):
        a = self.book(hour=10)
        pay.record_payment(a, Decimal("1000"), "cash", self.staff, "k1")
        Appointment.objects.filter(pk=a.pk).update(reminder_sent_at=timezone.now())
        moved = services.reschedule(a, future_slot(4, 14), self.staff)
        self.assertEqual((timezone.localtime(moved.start).hour, moved.duration_minutes, moved.reminder_sent_at), (14, 60, None))
        self.assertEqual(Payment.objects.filter(appointment=a).count(), 1)
        self.assertEqual(moved.events.filter(action="rescheduled").count(), 1)
        self.assertTrue(Notification.objects.filter(title="Session rescheduled", user=self.member_login).exists())

    def test_reschedule_conflicts_and_state(self):
        a = self.book(hour=10)
        b = self.book(hour=13, member=make_member_with_login(self.users, "Bob")[0])
        with self.assertRaisesMessage(BookingError, "trainer already has"):
            services.reschedule(a, future_slot(3, 13, 30), self.staff)
        services.reschedule(a, future_slot(3, 10, 30), self.staff)  # overlapping only with itself is fine
        services.cancel(b, self.staff, "x y z")
        with self.assertRaisesMessage(BookingError, "Only a scheduled"):
            services.reschedule(b, future_slot(5), self.staff)

    def test_member_notice_rule(self):
        near = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() + timedelta(hours=5),
                                          end=timezone.now() + timedelta(hours=6))
        with self.assertRaisesMessage(BookingError, "hours notice"):
            services.cancel(near, self.member_login, "Cannot make it", as_member=True)
        with self.assertRaisesMessage(BookingError, "hours notice"):
            services.reschedule(near, future_slot(4), self.member_login, as_member=True)
        services.cancel(near, self.staff, "Member phoned", force=False)  # staff are not bound by the member notice rule
        self.assertEqual(Appointment.objects.get(pk=near.pk).status, "cancelled")

    def test_member_can_cancel_with_notice_and_reason_is_required(self):
        a = self.book(days=4)
        with self.assertRaisesMessage(BookingError, "reason"):
            services.cancel(a, self.member_login, " ", as_member=True)
        services.cancel(a, self.member_login, "Travelling", as_member=True)
        a.refresh_from_db()
        self.assertEqual((a.status, a.cancel_reason, a.cancelled_by), ("cancelled", "Travelling", self.member_login))
        self.assertTrue(Notification.objects.filter(user=self.users["trainer"], title="Session cancelled").exists())
        with self.assertRaisesMessage(BookingError, "Only a scheduled"):
            services.cancel(a, self.staff, "again")

    def test_finishing_sessions(self):
        future = self.book()
        with self.assertRaisesMessage(BookingError, "not started"):
            services.finish(future, self.staff, Status.COMPLETED)
        past = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() - timedelta(hours=2),
                                          end=timezone.now() - timedelta(hours=1))
        self.assertEqual(past.status_key, "awaiting")
        services.finish(past, self.staff, Status.COMPLETED)
        past.refresh_from_db()
        self.assertEqual(past.status, "completed")
        with self.assertRaisesMessage(BookingError, "already has an outcome"):
            services.finish(past, self.staff, Status.NO_SHOW)


class SlotTests(Base):
    def test_available_slots_exclude_taken_and_respect_lead_time(self):
        self.book(hour=10)
        day = timezone.localdate() + timedelta(days=3)
        slots = [timezone.localtime(s).strftime("%H:%M") for s in services.available_slots(self.trainer, day, 60)]
        self.assertIn("09:00", slots)
        self.assertNotIn("09:15", slots)  # would overlap 10:00
        self.assertNotIn("10:00", slots)
        self.assertNotIn("10:45", slots)
        self.assertIn("11:00", slots)
        self.assertNotIn("19:15", slots)  # would end after 20:00
        today = timezone.localdate()
        for s in services.available_slots(self.trainer, today, 60, as_member=True):
            self.assertGreater(s, timezone.now() + timedelta(minutes=59))

    def test_slots_empty_for_time_off_inactive_and_bad_duration(self):
        day = timezone.localdate() + timedelta(days=3)
        self.assertEqual(services.available_slots(self.trainer, day, 25), [])
        TrainerTimeOff.objects.create(trainer=self.trainer, start_date=day, end_date=day)
        self.assertEqual(services.available_slots(self.trainer, day, 60), [])

    def test_slots_endpoint_scoping(self):
        day = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.login("trainer")
        r = self.client.get(reverse("appointments:slots"), {"trainer": self.trainer.pk, "date": day, "duration": 60})
        self.assertTrue(r.json()["slots"])
        other = make_trainer("Other Coach")
        self.assertEqual(self.client.get(reverse("appointments:slots"), {"trainer": other.pk, "date": day, "duration": 60}).status_code, 404)
        self.login("member")
        self.assertEqual(self.client.get(reverse("appointments:slots"), {"trainer": self.trainer.pk, "date": day, "duration": 60}).status_code, 403)
        self.assertEqual(self.client.get(reverse("appointments:slots")).status_code, 403)


class AppointmentViewTests(Base):
    def payload(self, **kw):
        slot = future_slot(3, 10)
        data = {"member": self.member.member_id, "trainer": self.trainer.pk, "date": slot.date().isoformat(), "time": "10:00", "duration": 60, "notes": ""}
        data.update(kw)
        return data

    def test_staff_booking_flow(self):
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("appointments:book")).status_code, 200)
        r = self.client.post(reverse("appointments:book"), self.payload())
        a = Appointment.objects.get()
        self.assertRedirects(r, a.get_absolute_url())
        r = self.client.post(reverse("appointments:book"), self.payload(member=make_member_with_login(self.users, "Bob")[0].member_id))
        self.assertContains(r, "trainer already has")
        r = self.client.post(reverse("appointments:book"), self.payload(member="GM999999"))
        self.assertContains(r, "No member found")
        self.assertEqual(Appointment.objects.count(), 1)

    def test_trainer_books_only_for_assigned_members_and_sees_only_own_sessions(self):
        stranger = make_member_with_login(self.users, "Stranger")[0]
        self.login("trainer")
        r = self.client.post(reverse("appointments:book"), self.payload(member=stranger.member_id))
        self.assertContains(r, "assigned to you")
        self.assertEqual(self.client.post(reverse("appointments:book"), self.payload()).status_code, 302)
        other_trainer = make_trainer("Other Coach")
        trainer_services.assign_trainer(other_trainer, stranger, self.users["manager"])
        theirs = services.book(stranger, other_trainer, future_slot(4), 60, self.users["manager"])
        mine = Appointment.objects.get(trainer=self.trainer)
        r = self.client.get(reverse("appointments:list"))
        self.assertEqual([a.pk for a in r.context["object_list"]], [mine.pk])
        self.assertEqual(self.client.get(theirs.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.post(reverse("appointments:cancel", args=[theirs.pk]), {"reason": "no no"}).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "scheduled")

    def test_permissions(self):
        a = self.book()
        for role, view, manage in (("manager", 200, 200), ("receptionist", 200, 200), ("trainer", 200, 200), ("accountant", 403, 403), ("member", 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("appointments:list")).status_code, view, role)
            self.assertEqual(self.client.get(reverse("appointments:book")).status_code, manage, role)
            self.assertEqual(self.client.get(reverse("appointments:reschedule", args=[a.pk])).status_code, manage, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("appointments:list")).status_code, 302)

    def test_reschedule_cancel_and_outcome_views(self):
        a = self.book()
        self.login("receptionist")
        r = self.client.post(reverse("appointments:reschedule", args=[a.pk]), {"date": future_slot(5).date().isoformat(), "time": "15:00"})
        self.assertRedirects(r, a.get_absolute_url())
        a.refresh_from_db()
        self.assertEqual(timezone.localtime(a.start).hour, 15)
        r = self.client.post(reverse("appointments:reschedule", args=[a.pk]), {"date": future_slot(5).date().isoformat(), "time": "04:00"})
        self.assertContains(r, "not available")
        self.assertEqual(self.client.post(reverse("appointments:complete", args=[a.pk]), follow=True).status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.status, "scheduled")  # not started yet
        self.assertEqual(self.client.get(reverse("appointments:complete", args=[a.pk])).status_code, 405)
        r = self.client.post(reverse("appointments:cancel", args=[a.pk]), {"reason": "Trainer unwell"})
        self.assertRedirects(r, a.get_absolute_url())
        a.refresh_from_db()
        self.assertEqual(a.status, "cancelled")

    def test_list_filters_and_tabs(self):
        upcoming = self.book(days=3)
        past = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() - timedelta(days=2),
                                          end=timezone.now() - timedelta(days=2) + timedelta(hours=1), status="completed")
        self.login("manager")
        ids = lambda **p: [a.pk for a in self.client.get(reverse("appointments:list"), p).context["object_list"]]
        self.assertEqual(ids(), [upcoming.pk])
        self.assertEqual(ids(when="past"), [past.pk])
        self.assertEqual(set(ids(when="all")), {upcoming.pk, past.pk})
        self.assertEqual(ids(when="all", status="completed"), [past.pk])
        self.assertEqual(ids(when="all", q="Alice"), ids(when="all"))
        self.assertEqual(ids(when="all", q="nobody"), [])

    def test_detail_page_renders_for_scheduled_cancelled_and_paid(self):
        a = self.book()
        pay.record_payment(a, Decimal("500"), "cash", self.staff, "kk")
        self.login("manager")
        r = self.client.get(a.get_absolute_url())
        self.assertContains(r, "Fee")
        self.assertContains(r, "Record payment")
        services.cancel(a, self.staff, "Test reason")
        self.assertContains(self.client.get(a.get_absolute_url()), "Test reason")


class ReminderTests(Base):
    def test_reminders_are_sent_once(self):
        self.member.email = "alice@example.com"
        self.member.save()
        near = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() + timedelta(hours=5),
                                          end=timezone.now() + timedelta(hours=6))
        far = self.book(days=10)
        out = StringIO()
        call_command("send_appointment_reminders", stdout=out)
        self.assertIn("1 reminder", out.getvalue())
        self.assertTrue(Notification.objects.filter(user=self.member_login, title="Session reminder").exists())
        self.assertTrue(Notification.objects.filter(user=self.users["trainer"], title="Session reminder").exists())
        # With only the development e-mail backend nothing external is queued, sent or claimed.
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(NotificationDelivery.objects.exists())
        near.refresh_from_db(), far.refresh_from_db()
        self.assertIsNotNone(near.reminder_sent_at)
        self.assertIsNone(far.reminder_sent_at)
        self.assertEqual(services.send_due_reminders(), 0)  # idempotent
        self.assertEqual(Notification.objects.filter(user=self.member_login, title="Session reminder").count(), 1)

    def test_cancelled_sessions_get_no_reminder(self):
        near = Appointment.objects.create(member=self.member, trainer=self.trainer, start=timezone.now() + timedelta(hours=5),
                                          end=timezone.now() + timedelta(hours=6), status="cancelled")
        self.assertEqual(services.send_due_reminders(), 0)


class AppointmentFeeTests(Base):
    def test_fee_uses_the_shared_payment_ledger(self):
        a = self.book(duration=60)  # LKR 3,000
        b = appointment_billing(a)
        self.assertEqual((b.amount_due, b.outstanding, b.payment_status), (Decimal("3000.00"), Decimal("3000.00"), "unpaid"))
        p1, _ = pay.record_payment(a, Decimal("1000"), "cash", self.staff, "a1")
        self.assertEqual((p1.appointment_id, p1.membership_id, p1.member_id), (a.pk, None, self.member.pk))
        self.assertEqual(appointment_billing(a).payment_status, "partial")
        with self.assertRaisesMessage(pay.PaymentError, "exceeds"):
            pay.record_payment(a, Decimal("2500"), "cash", self.staff, "a2")
        pay.record_payment(a, Decimal("2000"), "bank_transfer", self.staff, "a3", external_reference="SLIP")
        self.assertEqual(appointment_billing(a).payment_status, "paid")

    def test_duplicate_submission_does_not_duplicate_the_payment(self):
        a = self.book()
        first, c1 = pay.record_payment(a, Decimal("1000"), "cash", self.staff, "same")
        second, c2 = pay.record_payment(a, Decimal("1000"), "cash", self.staff, "same")
        self.assertEqual((c1, c2, first.pk), (True, False, second.pk))
        self.assertEqual(Payment.objects.count(), 1)
        mem = self.member.memberships.get()
        with self.assertRaisesMessage(pay.PaymentError, "different record"):
            pay.record_payment(mem, Decimal("100"), "cash", self.staff, "same")  # a key cannot jump between targets

    def test_payment_targets_exactly_one_item(self):
        a = self.book()
        mem = self.member.memberships.get()
        base = dict(member=self.member, amount=Decimal("5"), idempotency_key="x1", method="cash")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(**base)  # no target
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(membership=mem, appointment=a, **{**base, "idempotency_key": "x2"})  # both targets

    def test_cancelling_a_session_writes_off_unpaid_fee_and_blocks_payment(self):
        a = self.book()
        services.cancel(a, self.staff, "Illness")
        b = appointment_billing(a)
        self.assertEqual((b.outstanding, b.payment_status), (0, "unpaid"))
        with self.assertRaisesMessage(pay.PaymentError, "appointment was cancelled"):
            pay.record_payment(a, Decimal("100"), "cash", self.staff, "z1")

    def test_refund_and_adjustment_work_for_sessions(self):
        a = self.book()
        p, _ = pay.record_payment(a, Decimal("3000"), "cash", self.staff, "f1")
        pay.refund_payment(p, Decimal("1000"), "cash", self.users["manager"], "f2", "Cancelled by trainer")
        self.assertEqual(appointment_billing(a).payment_status, "partially_refunded")
        b = self.book(days=4)
        pay.adjust_charge(b, Decimal("-500"), self.users["manager"], "f3", "Loyalty")
        self.assertEqual(appointment_billing(b).amount_due, Decimal("2500.00"))

    def test_views_and_receipts_for_session_payments(self):
        a = self.book()
        self.login("receptionist")
        url = reverse("payments:record_appointment", args=[a.pk])
        page = self.client.get(url)
        key = page.context["form"].fields["idempotency_key"].initial
        data = {"amount": "3000", "method": "cash", "paid_on": timezone.localdate().isoformat(), "idempotency_key": key}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        payment = Payment.objects.get()
        receipt = self.client.get(reverse("payments:receipt", args=[payment.pk]))
        self.assertContains(receipt, "Personal training")
        self.assertContains(receipt, "Coach Kim")
        self.assertTrue(self.client.get(reverse("payments:receipt_pdf", args=[payment.pk])).content.startswith(b"%PDF"))
        self.assertContains(self.client.get(reverse("payments:list")), "Personal training")
        self.assertEqual(self.client.get(payment.get_absolute_url()).status_code, 200)
        self.login("trainer")  # trainers cannot take or see payments
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("payments:receipt", args=[payment.pk])).status_code, 403)

    def test_unpaid_session_fees_count_as_outstanding_not_revenue(self):
        a = self.book()
        membership_outstanding = Decimal("5000.00")
        s = get_dashboard_stats()
        self.assertEqual(s["stats"]["outstanding"], membership_outstanding + Decimal("3000.00"))
        self.assertEqual(s["stats"]["todays_revenue"], 0)
        pay.record_payment(a, Decimal("3000"), "cash", self.staff, "r1")
        s = get_dashboard_stats()
        self.assertEqual((s["stats"]["outstanding"], s["stats"]["todays_revenue"]), (membership_outstanding, Decimal("3000.00")))
        self.login("manager")
        self.assertEqual(self.client.get(reverse("payments:outstanding")).status_code, 200)

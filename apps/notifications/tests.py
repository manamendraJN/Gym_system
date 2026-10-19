import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from urllib.error import HTTPError

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.appointments import services as appointment_services
from apps.appointments.models import Appointment
from apps.core.models import Notification
from apps.core.testing import PASSWORD, future_slot, give_membership, make_member_with_login, make_trainer, make_users
from apps.memberships import services as membership_services
from apps.memberships.models import Membership, MembershipPlan
from apps.payments import services as pay
from apps.trainers import services as trainer_services

from . import delivery, services
from .models import Channel, DeliveryStatus, NotificationDelivery, NotificationPreference, NotificationSettings, parse_days

SMTP = "django.core.mail.backends.smtp.EmailBackend"
TWILIO = dict(SMS_PROVIDER="twilio", WHATSAPP_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC123", TWILIO_AUTH_TOKEN="secret-token",
              TWILIO_SMS_FROM="+15550001111", TWILIO_WHATSAPP_FROM="whatsapp:+15550002222",
              TWILIO_WEBHOOK_URL="https://gym.example/notifications/webhooks/twilio/")


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.manager = self.users["manager"]
        self.member, self.login_user = make_member_with_login(self.users, "Alice Member", membership=False)
        self.member.email = "alice@example.com"
        self.member.save()
        self.login_user.email = "alice@example.com"
        self.login_user.save()
        self.plan = MembershipPlan.objects.create(name="Monthly", price=Decimal("5000"))

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def membership(self, ends_in, started_ago=20, **kw):
        today = timezone.localdate()
        return Membership.objects.create(member=kw.pop("member", self.member), plan=self.plan, start_date=today - timedelta(days=started_ago),
                                         end_date=today + timedelta(days=ends_in), price=Decimal("5000"), **kw)

    def mine(self, kind):
        return Notification.objects.filter(user=self.login_user, kind=kind)


class DedupeTests(Base):
    def test_same_key_creates_one_notification_per_user(self):
        a = Notification.send(self.login_user, "t", "m", dedupe_key="k1")
        self.assertIsNotNone(a)
        self.assertIsNone(Notification.send(self.login_user, "t", "m", dedupe_key="k1"))
        self.assertIsNotNone(Notification.send(self.manager, "t", "m", dedupe_key="k1"))  # another user may get their own
        self.assertIsNotNone(Notification.send(self.login_user, "t", "m"))  # no key = never deduplicated
        self.assertIsNotNone(Notification.send(self.login_user, "t", "m"))

    def test_database_constraint_is_the_final_guard(self):
        Notification.send(self.login_user, "t", "m", dedupe_key="k2")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(user=self.login_user, title="x", message="y", dedupe_key="k2")

    def test_inactive_or_missing_users_get_nothing(self):
        self.login_user.is_active = False
        self.login_user.save()
        self.assertIsNone(Notification.send(self.login_user, "t", "m"))
        self.assertIsNone(Notification.send(None, "t", "m"))


class SettingsParsingTests(TestCase):
    def test_parse_days(self):
        self.assertEqual(parse_days("7, 14,1,7"), [14, 7, 1])
        self.assertEqual(parse_days("3,0,-3", allow_negative=True), [3, 0, -3])
        for bad in ("", "a,b", "-1", "999"):
            with self.assertRaises(ValueError):
                parse_days(bad)


class MembershipReminderTests(Base):
    def test_expiry_reminders_follow_the_configured_periods_without_repeats(self):
        today = timezone.localdate()
        m = self.membership(ends_in=10)
        services.membership_expiry_reminders(today)  # 10 days left: the 14-day period has been reached
        self.assertEqual(self.mine("membership-expiring").count(), 1)
        self.assertEqual(services.membership_expiry_reminders(today), 0)  # repeating the run changes nothing
        services.membership_expiry_reminders(today + timedelta(days=2))
        self.assertEqual(self.mine("membership-expiring").count(), 1)  # 8 days left: still the 14-day band
        services.membership_expiry_reminders(today + timedelta(days=4))  # 6 days left -> 7-day band
        self.assertEqual(self.mine("membership-expiring").count(), 2)
        services.membership_expiry_reminders(today + timedelta(days=10))  # ends today -> 1-day band
        self.assertEqual(self.mine("membership-expiring").count(), 3)
        services.membership_expiry_reminders(today + timedelta(days=10))
        self.assertEqual(self.mine("membership-expiring").count(), 3)
        keys = set(self.mine("membership-expiring").values_list("dedupe_key", flat=True))
        self.assertEqual(keys, {f"membership-expiring:{m.pk}:{o}" for o in (14, 7, 1)})

    def test_periods_are_configurable(self):
        conf = NotificationSettings.load()
        conf.membership_expiry_days = "30"
        conf.save()
        self.membership(ends_in=25)
        services.membership_expiry_reminders()
        self.assertEqual(self.mine("membership-expiring").count(), 1)
        self.assertTrue(self.mine("membership-expiring").get().dedupe_key.endswith(":30"))

    def test_renewed_memberships_are_not_reminded(self):
        m = self.membership(ends_in=5)
        membership_services.renew_membership(m, self.manager)
        services.membership_expiry_reminders()
        self.assertEqual(self.mine("membership-expiring").count(), 0)

    def test_members_without_login_and_inactive_members_are_skipped_but_staff_still_get_one_digest(self):
        other = make_member_with_login(self.users, "Bob", membership=False)[0]
        other.user.delete()
        self.membership(ends_in=3, member=other)
        created = services.membership_expiry_reminders()
        self.assertEqual(created, 3)  # staff digest only: super admin, manager, receptionist (memberships.manage)
        self.assertEqual(Notification.objects.filter(kind="membership-expiring", user=self.manager).count(), 1)
        self.assertEqual(services.membership_expiry_reminders(), 0)

    def test_expired_notice_once_and_only_for_recent_expiry(self):
        today = timezone.localdate()
        recent = self.membership(ends_in=-2, started_ago=40)
        old = self.membership(ends_in=-30, started_ago=70, member=make_member_with_login(self.users, "Old", membership=False)[0])
        services.membership_expired_notices(today)
        self.assertEqual(self.mine("membership-expired").count(), 1)
        self.assertEqual(Notification.objects.filter(kind="membership-expired", user=old.member.user).count(), 0)
        services.membership_expired_notices(today)
        self.assertEqual(self.mine("membership-expired").count(), 1)
        self.assertEqual(self.mine("membership-expired").get().dedupe_key, f"membership-expired:{recent.pk}")

    def test_renewal_and_start_notifications(self):
        today = timezone.localdate()
        m = membership_services.assign_membership(self.member, self.plan, today, self.manager)
        self.assertEqual(self.mine("membership-started").count(), 1)
        renewed = membership_services.renew_membership(m, self.manager)
        note = self.mine("membership-renewed").get()
        self.assertEqual(note.dedupe_key, f"membership-renewed:{renewed.pk}")
        self.assertIn(f"{renewed.start_date:%b %d, %Y}", note.message)


class PaymentNotificationTests(Base):
    def test_payment_recording_confirmation_and_due_reminders(self):
        today = timezone.localdate()
        m = self.membership(ends_in=20, started_ago=10)  # unpaid, due 3 days ago (7-day grace)
        services.payment_due_reminders(today)
        due = self.mine("payment-due").get()
        self.assertEqual((due.title, due.dedupe_key), ("Payment overdue", f"payment-due:{m.pk}:-3"))
        self.assertIn("5,000.00", due.message)
        self.assertEqual(services.payment_due_reminders(today), 0)
        services.payment_due_reminders(today + timedelta(days=5))  # 8 days overdue -> last band
        self.assertEqual(self.mine("payment-due").count(), 2)
        services.payment_due_reminders(today + timedelta(days=30))
        self.assertEqual(self.mine("payment-due").count(), 2)  # no endless nagging: the last period is sent once
        # recording a payment notifies once (pending -> awaiting, confirm -> confirmed)
        p, created = pay.record_payment(m, Decimal("2000"), "bank_transfer", self.users["receptionist"], "k1", pending=True)
        self.assertEqual(self.mine("payment-recorded").get().title, "Payment awaiting confirmation")
        pay.record_payment(m, Decimal("2000"), "bank_transfer", self.users["receptionist"], "k1", pending=True)  # duplicate submit
        self.assertEqual(self.mine("payment-recorded").count(), 1)
        pay.confirm_payment(p, self.users["accountant"])
        self.assertEqual(sorted(self.mine("payment-recorded").values_list("title", flat=True)), ["Payment awaiting confirmation", "Payment confirmed"])
        p2, _ = pay.record_payment(m, Decimal("3000"), "cash", self.users["receptionist"], "k2")
        self.assertTrue(self.mine("payment-recorded").filter(title="Payment received", message__contains=p2.receipt_number).exists())

    def test_paid_up_memberships_get_no_due_reminder_and_cancelled_ones_neither(self):
        paid = self.membership(ends_in=20, started_ago=10)
        pay.record_payment(paid, Decimal("5000"), "cash", self.users["receptionist"], "pp")
        other = make_member_with_login(self.users, "Cara", membership=False)[0]
        cancelled = self.membership(ends_in=20, started_ago=10, member=other)
        membership_services.cancel_membership(cancelled, self.manager, "Left")
        self.assertEqual(services.payment_due_reminders(), 0)

    def test_staff_get_one_weekly_digest_for_overdue_balances(self):
        self.membership(ends_in=20, started_ago=30)
        services.payment_due_reminders()
        count = Notification.objects.filter(kind="payment-due", user=self.users["accountant"]).count()
        services.payment_due_reminders()
        self.assertEqual(Notification.objects.filter(kind="payment-due", user=self.users["accountant"]).count(), count)
        self.assertEqual(Notification.objects.filter(kind="payment-due", user=self.manager).count(), 1)

    def test_due_period_settings_are_respected(self):
        conf = NotificationSettings.load()
        conf.payment_due_grace_days, conf.payment_due_offsets = 30, "5"
        conf.save()
        self.membership(ends_in=20, started_ago=26)  # due in 4 days
        services.payment_due_reminders()
        self.assertEqual(self.mine("payment-due").get().title, "Payment due")


class AppointmentReminderTests(Base):
    def test_reminder_window_is_configurable_and_never_repeated(self):
        trainer = make_trainer("Coach", user=self.users["trainer"])
        trainer_services.assign_trainer(trainer, self.member, self.manager)
        near = Appointment.objects.create(member=self.member, trainer=trainer, start=timezone.now() + timedelta(hours=5), end=timezone.now() + timedelta(hours=6))
        conf = NotificationSettings.load()
        conf.appointment_reminder_hours = 2
        conf.save()
        self.assertEqual(appointment_services.send_due_reminders(), 0)
        conf.appointment_reminder_hours = 24
        conf.save()
        self.assertEqual(appointment_services.send_due_reminders(), 1)
        self.assertEqual(appointment_services.send_due_reminders(), 0)
        self.assertEqual(self.mine("appointment-reminder").count(), 1)
        near.refresh_from_db()
        self.assertIsNotNone(near.reminder_sent_at)
        # rescheduling re-arms the reminder with a different key (new start time)
        near.reminder_sent_at = None
        near.start += timedelta(hours=1)
        near.end += timedelta(hours=1)
        near.save()
        self.assertEqual(appointment_services.send_due_reminders(), 1)
        self.assertEqual(self.mine("appointment-reminder").count(), 2)


class RunAllTests(Base):
    def test_command_runs_everything_and_is_idempotent(self):
        self.membership(ends_in=3)
        out = StringIO()
        call_command("run_scheduled_notifications", stdout=out)
        self.assertIn("membership expiring: ", out.getvalue())
        before = Notification.objects.count()
        self.assertGreater(before, 0)
        call_command("run_scheduled_notifications", stdout=StringIO())
        self.assertEqual(Notification.objects.count(), before)

    def test_run_all_reports_each_generator(self):
        results = services.run_all()
        for key in ("announcements", "membership_expiring", "membership_expired", "payment_due", "appointments", "equipment", "low_stock", "deliveries"):
            self.assertIn(key, results)


class DeliveryHonestyTests(Base):
    def test_nothing_external_without_configuration(self):
        note = Notification.send(self.login_user, "Hi", "Hello", external=True)
        self.assertEqual(NotificationDelivery.objects.count(), 0)  # dev e-mail backend, no SMS/WhatsApp provider
        self.assertEqual(delivery.process_queue(), {s: 0 for s in DeliveryStatus.values})
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(any(delivery.configured_channels().values()))

    @override_settings(EMAIL_BACKEND=SMTP)
    def test_email_is_reported_as_sent_never_as_delivered(self):
        note = Notification.send(self.login_user, "Hi", "Hello", external=True)
        d = NotificationDelivery.objects.get()
        self.assertEqual((d.channel, d.address, d.status), (Channel.EMAIL, "alice@example.com", DeliveryStatus.QUEUED))  # queued, not yet sent
        with mock.patch(f"{SMTP}.send_messages", return_value=1) as send:
            counts = delivery.process_queue()
        d.refresh_from_db()
        self.assertEqual(send.call_count, 1)
        self.assertEqual((d.status, d.delivered_at, counts["sent"]), (DeliveryStatus.SENT, None, 1))
        self.assertIn("not confirmed", d.detail)

    @override_settings(EMAIL_BACKEND=SMTP)
    def test_email_failures_are_retried_then_reported_as_failed(self):
        Notification.send(self.login_user, "Hi", "Hello", external=True)
        d = NotificationDelivery.objects.get()
        with mock.patch(f"{SMTP}.send_messages", side_effect=OSError("smtp down")):
            for attempt in (1, 2):
                delivery.process_queue()
                d.refresh_from_db()
                self.assertEqual((d.status, d.attempts), (DeliveryStatus.QUEUED, attempt))
            delivery.process_queue()
        d.refresh_from_db()
        self.assertEqual((d.status, d.attempts), (DeliveryStatus.FAILED, 3))
        self.assertIn("OSError", d.detail)
        self.assertNotIn("smtp down", d.detail)  # internal error text is not exposed

    @override_settings(EMAIL_BACKEND=SMTP)
    def test_user_and_business_switches_are_respected(self):
        NotificationPreference.objects.update_or_create(user=self.login_user, defaults={"email_enabled": False})
        Notification.send(self.login_user, "Hi", "Hello", external=True)
        self.assertEqual(NotificationDelivery.objects.count(), 0)
        NotificationPreference.objects.filter(user=self.login_user).update(email_enabled=True)
        conf = NotificationSettings.load()
        conf.email_enabled = False
        conf.save()
        Notification.send(self.login_user, "Hi 2", "Hello", external=True)
        self.assertEqual(NotificationDelivery.objects.count(), 0)
        conf.email_enabled = True
        conf.save()
        no_address = self.users["trainer"]
        no_address.email = ""
        no_address.save()
        Notification.send(no_address, "Hi 3", "Hello", external=True)
        self.assertEqual(NotificationDelivery.objects.get().status, DeliveryStatus.SKIPPED)
        self.assertEqual(delivery.process_queue()["sent"], 0)  # skipped rows are never attempted

    @override_settings(EMAIL_BACKEND=SMTP)
    def test_internal_only_notifications_are_never_copied_externally(self):
        Notification.send(self.login_user, "Internal", "Staff only note")
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    @override_settings(**TWILIO)
    def test_sms_and_whatsapp_need_opt_in_then_are_sent_through_twilio(self):
        self.login_user.phone = "0771234567"
        self.login_user.save()
        conf = NotificationSettings.load()
        conf.sms_enabled = conf.whatsapp_enabled = True
        conf.save()
        Notification.send(self.login_user, "Hi", "Hello", external=True)
        self.assertEqual(NotificationDelivery.objects.count(), 0)  # members must opt in to SMS / WhatsApp
        NotificationPreference.objects.update_or_create(user=self.login_user, defaults={"sms_enabled": True, "whatsapp_enabled": True})
        Notification.send(self.login_user, "Hi again", "Hello", external=True)
        rows = {d.channel: d for d in NotificationDelivery.objects.all()}
        self.assertEqual(set(rows), {Channel.SMS, Channel.WHATSAPP})
        self.assertEqual(rows[Channel.SMS].address, "+94771234567")
        with mock.patch("apps.notifications.delivery.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({"sid": "SM1", "status": "queued"}).encode()
            delivery.process_queue()
        sent = urlopen.call_args_list[0][0][0]
        self.assertIn("AC123/Messages.json", sent.full_url)
        self.assertIn(b"To=%2B94771234567", sent.data)
        self.assertIn(b"StatusCallback=", sent.data)
        for d in NotificationDelivery.objects.all():
            self.assertEqual((d.status, d.delivered_at), (DeliveryStatus.SENT, None))  # accepted != delivered
        wa = NotificationDelivery.objects.get(channel=Channel.WHATSAPP)
        self.assertEqual(wa.provider_message_id, "SM1")

    @override_settings(**TWILIO)
    def test_provider_rejection_is_failed_not_sent(self):
        self.login_user.phone = "0771234567"
        self.login_user.save()
        NotificationSettings.objects.update_or_create(pk=1, defaults={"sms_enabled": True})
        NotificationPreference.objects.update_or_create(user=self.login_user, defaults={"sms_enabled": True})
        Notification.send(self.login_user, "Hi", "Hello", external=True)
        err = HTTPError("https://api.twilio.com", 400, "Bad Request", {}, None)
        with mock.patch("apps.notifications.delivery.urllib.request.urlopen", side_effect=err):
            delivery.process_queue()
        d = NotificationDelivery.objects.get()
        self.assertEqual((d.status, d.attempts), (DeliveryStatus.FAILED, 1))
        self.assertIn("HTTP 400", d.detail)

    def test_phone_normalisation(self):
        self.login_user.phone = "077 123 4567"
        self.assertEqual(delivery.phone_for(self.login_user), "+94771234567")
        self.login_user.phone = "+44 20 7946 0958"
        self.assertEqual(delivery.phone_for(self.login_user), "+442079460958")
        self.login_user.phone = ""
        self.login_user.save()
        self.assertEqual(delivery.phone_for(self.login_user), "+94" + self.member.phone[1:])  # falls back to the member record


@override_settings(**TWILIO)
class WebhookTests(Base):
    URL = TWILIO["TWILIO_WEBHOOK_URL"]

    def setUp(self):
        super().setUp()
        note = Notification.send(self.login_user, "Hi", "Hello")
        self.d = NotificationDelivery.objects.create(notification=note, channel=Channel.SMS, address="+94771234567", status=DeliveryStatus.SENT, provider_message_id="SM42")

    def post(self, params, signature=None, path=None):
        sig = signature if signature is not None else delivery.twilio_signature(self.URL, params, "secret-token")
        return self.client.post(path or reverse("notifications:twilio_webhook"), params, HTTP_X_TWILIO_SIGNATURE=sig)

    def test_signed_callback_marks_delivered(self):
        self.assertEqual(self.post({"MessageSid": "SM42", "MessageStatus": "delivered"}).status_code, 204)
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, DeliveryStatus.DELIVERED)
        self.assertIsNotNone(self.d.delivered_at)

    def test_failed_callback_and_intermediate_states(self):
        self.post({"MessageSid": "SM42", "MessageStatus": "sending"})
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, DeliveryStatus.SENT)  # not delivered yet
        self.post({"MessageSid": "SM42", "MessageStatus": "undelivered"})
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, DeliveryStatus.FAILED)

    def test_unsigned_or_wrongly_signed_callbacks_are_rejected(self):
        params = {"MessageSid": "SM42", "MessageStatus": "delivered"}
        self.assertEqual(self.post(params, signature="").status_code, 403)
        self.assertEqual(self.post(params, signature="forged").status_code, 403)
        self.assertEqual(self.post({**params, "MessageStatus": "failed"}, signature=delivery.twilio_signature(self.URL, params, "secret-token")).status_code, 403)  # tampered body
        self.d.refresh_from_db()
        self.assertEqual(self.d.status, DeliveryStatus.SENT)

    def test_unknown_message_ids_are_ignored_and_get_is_not_allowed(self):
        self.assertEqual(self.post({"MessageSid": "SM-unknown", "MessageStatus": "delivered"}).status_code, 204)
        self.assertEqual(self.client.get(reverse("notifications:twilio_webhook")).status_code, 405)

    @override_settings(TWILIO_AUTH_TOKEN="")
    def test_endpoint_does_not_exist_without_a_token(self):
        self.assertEqual(self.client.post(reverse("notifications:twilio_webhook"), {"MessageSid": "SM42", "MessageStatus": "delivered"}).status_code, 404)


class SettingsAndPreferenceViewTests(Base):
    def test_only_authorised_staff_configure_reminder_periods(self):
        url = reverse("notifications:settings")
        for role in ("receptionist", "trainer", "accountant", "member"):
            self.login(self.users[role])
            self.assertEqual(self.client.get(url).status_code, 403, role)
        self.login(self.manager)
        page = self.client.get(url)
        self.assertContains(page, "Not configured")
        data = {"membership_expiry_days": "30, 10,2", "payment_due_grace_days": 5, "payment_due_offsets": "2,0,-1", "appointment_reminder_hours": 12,
                "maintenance_reminder_days": "14,3", "email_enabled": "on"}
        self.assertRedirects(self.client.post(url, data), url)
        conf = NotificationSettings.load()
        self.assertEqual((conf.expiry_days(), conf.due_offsets(), conf.maintenance_days(), conf.appointment_reminder_hours, conf.updated_by), ([30, 10, 2], [2, 0, -1], [14, 3], 12, self.manager))
        self.assertFalse(conf.sms_enabled)
        for bad in ({"membership_expiry_days": "abc"}, {"membership_expiry_days": "-5"}, {"payment_due_offsets": ""}, {"appointment_reminder_hours": 0}, {"maintenance_reminder_days": "1000"}):
            self.assertEqual(self.client.post(url, {**data, **bad}).status_code, 200)
        self.assertEqual(NotificationSettings.load().expiry_days(), [30, 10, 2])

    def test_every_user_manages_their_own_preferences(self):
        url = reverse("notifications:preferences")
        self.login(self.login_user)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertRedirects(self.client.post(url, {"sms_enabled": "on"}), url)
        pref = NotificationPreference.objects.get(user=self.login_user)
        self.assertEqual((pref.email_enabled, pref.sms_enabled, pref.whatsapp_enabled), (False, True, False))
        self.assertFalse(NotificationPreference.objects.filter(user=self.manager).exists())  # nobody else's row was touched
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_notification_centre_shows_history_filters_and_delivery_status(self):
        n1 = Notification.send(self.login_user, "Old news", "m", kind="announcement")
        n2 = Notification.send(self.login_user, "Fresh", "m", kind="payment-due")
        NotificationDelivery.objects.create(notification=n2, channel=Channel.EMAIL, address="a@b.c", status=DeliveryStatus.SENT, detail="Accepted by the mail server.")
        n1.read_at = timezone.now()
        n1.save()
        self.login(self.login_user)
        page = self.client.get(reverse("core:notifications"))
        self.assertContains(page, "Old news")
        self.assertContains(page, "in-app: generated")
        self.assertContains(page, "E-mail: Handed to provider (not confirmed delivered)")
        self.assertNotContains(page, "Delivered (confirmed")
        names = lambda **p: {n.title for n in self.client.get(reverse("core:notifications"), p).context["object_list"]}
        self.assertEqual(names(unread=1), {"Fresh"})
        self.assertEqual(names(kind="announcement"), {"Old news"})


class CeleryConfigTests(TestCase):
    def test_tasks_are_plain_functions_and_celery_config_is_valid_when_installed(self):
        from apps.notifications import tasks

        self.assertTrue(callable(tasks.run_scheduled_notifications))
        try:
            import celery  # noqa: F401
        except ImportError:
            self.skipTest("Celery is optional and not installed")
        from config.celery import app

        self.assertEqual(set(app.conf.beat_schedule), {"scheduled-notifications", "deliver-queued-messages"})
        self.assertIsNotNone(tasks.run_scheduled_notifications_task)
        self.assertEqual(tasks.run_scheduled_notifications_task.name, "notifications.run_scheduled")

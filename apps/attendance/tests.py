from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone

UTC = dt_timezone.utc
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.core.stats import get_dashboard_stats
from apps.core.testing import assign_to_trainer
from apps.members.models import Member
from apps.members.tests import make_member
from apps.memberships import services as membership_services
from apps.memberships.models import DurationType, Membership, MembershipPlan

from . import services
from .models import Attendance
from .services import CheckInDenied

PASSWORD = "Str0ng-Passw0rd!"
TUESDAY = date(2026, 3, 10)  # a Tuesday


def at(day, hour=10, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = {r: User.objects.create_user(username=r, email=f"{r}@example.com", password=PASSWORD, role=r) for r in Role.values}
        self.staff = self.users["receptionist"]
        self.plan = MembershipPlan.objects.create(name="Monthly", price=5000)
        self.member = make_member(registration_date=TUESDAY)
        self.membership = self.give_membership(self.member, self.plan)

    def give_membership(self, member, plan, start=TUESDAY):
        with mock.patch("django.utils.timezone.localdate", return_value=start):
            return membership_services.assign_membership(member, plan, start, self.users["manager"])

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))


class CheckInRuleTests(Base):
    def test_valid_check_in_and_check_out(self):
        visit = services.check_in(self.member, self.staff, now=at(TUESDAY))
        self.assertEqual((visit.method, visit.membership_id, visit.checked_in_by), ("manual", self.membership.pk, self.staff))
        out = services.check_out(visit, self.staff, now=at(TUESDAY, 11, 30))
        self.assertEqual(out.duration, timedelta(minutes=90))
        services.check_in(self.member, self.staff, now=at(TUESDAY, 15))  # a second visit the same day is fine once checked out
        self.assertEqual(self.member.attendance.count(), 2)

    def test_duplicate_check_in_is_refused(self):
        services.check_in(self.member, self.staff, now=at(TUESDAY))
        with self.assertRaisesMessage(CheckInDenied, "Already checked in"):
            services.check_in(self.member, self.staff, now=at(TUESDAY, 10, 5))
        self.assertEqual(self.member.attendance.count(), 1)

    def test_database_blocks_two_open_visits(self):
        Attendance.objects.create(member=self.member, check_in=at(TUESDAY))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attendance.objects.create(member=self.member, check_in=at(TUESDAY, 11))

    def test_check_out_before_check_in_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attendance.objects.create(member=self.member, check_in=at(TUESDAY), check_out=at(TUESDAY, 9))

    def test_double_check_out_refused(self):
        visit = services.check_in(self.member, self.staff, now=at(TUESDAY))
        services.check_out(visit, self.staff, now=at(TUESDAY, 11))
        with self.assertRaisesMessage(CheckInDenied, "already checked out"):
            services.check_out(visit, self.staff)

    def test_membership_state_is_validated(self):
        other = make_member()
        with self.assertRaisesMessage(CheckInDenied, "no active membership"):
            services.check_in(other, self.staff, now=at(TUESDAY))
        with self.assertRaisesMessage(CheckInDenied, "expired on"):
            services.check_in(self.member, self.staff, now=at(TUESDAY + timedelta(days=60)))
        with self.assertRaisesMessage(CheckInDenied, "starts on"):
            services.check_in(self.member, self.staff, now=at(TUESDAY - timedelta(days=3)))
        with mock.patch("django.utils.timezone.localdate", return_value=TUESDAY):
            membership_services.suspend_membership(self.membership, self.users["manager"], "Injury")
        with self.assertRaisesMessage(CheckInDenied, "suspended"):
            services.check_in(self.member, self.staff, now=at(TUESDAY))

    def test_cancelled_and_deactivated(self):
        with mock.patch("django.utils.timezone.localdate", return_value=TUESDAY):
            membership_services.cancel_membership(self.membership, self.users["manager"], "Moved")
        with self.assertRaises(CheckInDenied):
            services.check_in(self.member, self.staff, now=at(TUESDAY))
        m2 = make_member()
        self.give_membership(m2, self.plan)
        m2.is_active = False
        m2.save()
        with self.assertRaisesMessage(CheckInDenied, "deactivated"):
            services.check_in(m2, self.staff, now=at(TUESDAY))

    def test_plan_weekday_and_hour_rules(self):
        plan = MembershipPlan.objects.create(name="Weekday off-peak", price=3000, duration_type=DurationType.ANNUAL,
                                             allowed_weekdays="0,1,2,3,4", access_from=time(6), access_until=time(18))
        m = make_member()
        self.give_membership(m, plan)
        services.check_in(m, self.staff, now=at(TUESDAY, 9))  # Tuesday morning: ok
        services.check_out(m.attendance.get(), self.staff, now=at(TUESDAY, 10))
        with self.assertRaisesMessage(CheckInDenied, "between 06:00 and 18:00"):
            services.check_in(m, self.staff, now=at(TUESDAY, 19))
        with self.assertRaisesMessage(CheckInDenied, "only allows access on Mon"):
            services.check_in(m, self.staff, now=at(TUESDAY + timedelta(days=4), 9))  # Saturday

    def test_local_time_is_used_for_rules(self):
        plan = MembershipPlan.objects.create(name="Early", price=1, access_from=time(5), access_until=time(7))
        m = make_member()
        self.give_membership(m, plan)
        # 00:30 UTC is 06:00 in Colombo (UTC+5:30): allowed. 06:30 UTC is 12:00 local: refused.
        ok = timezone.make_aware(datetime(2026, 3, 10, 0, 30), UTC)
        services.check_in(m, self.staff, now=ok)
        services.check_out(m.attendance.get(), self.staff, now=ok + timedelta(minutes=30))
        with self.assertRaises(CheckInDenied):
            services.check_in(m, self.staff, now=timezone.make_aware(datetime(2026, 3, 10, 6, 30), UTC))

    def test_stale_open_visit_is_closed_automatically(self):
        old = Attendance.objects.create(member=self.member, check_in=at(TUESDAY, 6))
        visit = services.check_in(self.member, self.staff, now=at(TUESDAY, 22))  # 16h later
        old.refresh_from_db()
        self.assertTrue(old.auto_closed)
        self.assertEqual(old.check_out, old.check_in + timedelta(hours=12))
        self.assertTrue(visit.is_open)

    @override_settings(ATTENDANCE_BLOCK_UNPAID=True)
    def test_optional_unpaid_block(self):
        from apps.payments import services as pay

        with self.assertRaisesMessage(CheckInDenied, "No payment"):
            services.check_in(self.member, self.staff, now=at(TUESDAY))
        with mock.patch("django.utils.timezone.localdate", return_value=TUESDAY):
            pay.record_payment(self.membership, 1000, "cash", self.staff, "k1", paid_on=TUESDAY)
        services.check_in(self.member, self.staff, now=at(TUESDAY))


class QRTests(Base):
    def test_qr_payload_has_no_personal_data(self):
        payload = services.qr_payload(self.member)
        for secret in (self.member.full_name, self.member.phone, self.member.member_id, self.member.email):
            if secret:
                self.assertNotIn(secret.lower(), payload.lower())
        self.assertTrue(payload.startswith("GYMQR:"))
        self.assertGreaterEqual(len(self.member.qr_token), 40)

    def test_tokens_are_unique_and_random(self):
        tokens = {make_member().qr_token for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    def test_scan_toggles_check_in_and_out(self):
        action, visit, member = services.scan_qr(services.qr_payload(self.member), self.staff, now=at(TUESDAY))
        self.assertEqual((action, member, visit.method), ("in", self.member, "qr"))
        action, visit, _ = services.scan_qr(self.member.qr_token, self.staff, now=at(TUESDAY, 11))  # raw token works too
        self.assertEqual(action, "out")
        self.assertIsNotNone(visit.check_out)

    def test_scan_applies_membership_rules(self):
        other = make_member()
        with self.assertRaisesMessage(CheckInDenied, "no active membership"):
            services.scan_qr(other.qr_token, self.staff, now=at(TUESDAY))

    @override_settings(QR_SCAN_MAX_FAILURES=3)
    def test_unknown_codes_are_rejected_and_rate_limited(self):
        for _ in range(3):
            with self.assertRaisesMessage(CheckInDenied, "Unrecognised"):
                services.scan_qr("x" * 43, self.staff)
        with self.assertRaisesMessage(CheckInDenied, "Too many"):
            services.scan_qr(self.member.qr_token, self.staff)  # even a valid code waits until the window passes

    def test_short_or_empty_codes_never_hit_the_database_match(self):
        for bad in ("", "abc", "GYMQR:", None):
            with self.assertRaises(CheckInDenied):
                services.scan_qr(bad, self.staff)

    def test_regenerating_invalidates_old_code(self):
        old = self.member.qr_token
        self.login("receptionist")
        self.client.post(reverse("attendance:qr_regenerate", args=[self.member.pk]))
        self.member.refresh_from_db()
        self.assertNotEqual(self.member.qr_token, old)
        with self.assertRaises(CheckInDenied):
            services.scan_qr(old, self.staff, now=at(TUESDAY))

    def test_qr_image_and_card_permissions(self):
        url = reverse("attendance:qr_image", args=[self.member.pk])
        self.login("receptionist")
        r = self.client.get(url)
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        self.assertIn("no-store", r["Cache-Control"])
        self.assertTrue(r.content.startswith(b"\x89PNG"))
        for role in ("trainer", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, 403, role)
            self.assertEqual(self.client.get(reverse("attendance:qr_card", args=[self.member.pk])).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_scan_page_flow(self):
        today = timezone.localdate()
        m = make_member()
        self.give_membership(m, self.plan, today)
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("attendance:scan")).status_code, 200)
        r = self.client.post(reverse("attendance:scan"), {"code": services.qr_payload(m)})
        self.assertContains(r, "Checked in")
        r = self.client.post(reverse("attendance:scan"), {"code": services.qr_payload(m)})
        self.assertContains(r, "Checked out")
        r = self.client.post(reverse("attendance:scan"), {"code": "GYMQR:" + "z" * 43})
        self.assertContains(r, "Unrecognised", status_code=422)
        self.assertNotContains(r, m.full_name, status_code=422)  # unknown codes reveal nothing
        self.login("trainer")
        self.assertEqual(self.client.post(reverse("attendance:scan"), {"code": m.qr_token}).status_code, 403)


class AttendanceViewTests(Base):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.live = make_member(full_name="Live Person", phone="0775550001")
        self.give_membership(self.live, self.plan, self.today)

    def test_desk_search_by_id_name_phone(self):
        self.login("receptionist")
        for q in (self.live.member_id, "Live Per", "0775550001", "775550001"):
            r = self.client.get(reverse("attendance:desk"), {"q": q})
            self.assertEqual([m.pk for m in r.context["results"]], [self.live.pk], q)

    def test_manual_check_in_and_out_through_views(self):
        self.login("receptionist")
        r = self.client.post(reverse("attendance:check_in", args=[self.live.pk]), follow=True)
        self.assertContains(r, "checked in")
        r = self.client.post(reverse("attendance:check_in", args=[self.live.pk]), follow=True)
        self.assertContains(r, "Already checked in")
        self.assertEqual(Attendance.objects.count(), 1)
        visit = Attendance.objects.get()
        r = self.client.post(reverse("attendance:check_out", args=[visit.pk]), follow=True)
        self.assertContains(r, "checked out")
        self.assertEqual(self.client.get(reverse("attendance:check_in", args=[self.live.pk])).status_code, 405)

    def test_refused_check_in_is_explained(self):
        other = make_member(full_name="No Plan")
        self.login("receptionist")
        r = self.client.post(reverse("attendance:check_in", args=[other.pk]), follow=True)
        self.assertContains(r, "Check-in refused")
        self.assertFalse(Attendance.objects.exists())

    def test_permissions(self):
        for role, view, manage in (("trainer", 200, 403), ("receptionist", 200, 200), ("member", 403, 403), ("accountant", 403, 403)):
            self.login(role)
            for name in ("desk", "history", "report"):
                self.assertEqual(self.client.get(reverse(f"attendance:{name}")).status_code, view, (role, name))
            self.assertEqual(self.client.post(reverse("attendance:check_in", args=[self.live.pk])).status_code,
                             302 if manage == 200 else 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("attendance:history")).status_code, 302)

    def test_history_filters_and_member_history(self):
        a = Attendance.objects.create(member=self.live, check_in=at(date(2026, 1, 5)), check_out=at(date(2026, 1, 5), 11))
        b = Attendance.objects.create(member=self.live, check_in=at(date(2026, 2, 5)), check_out=at(date(2026, 2, 5), 11), method="qr")
        c = Attendance.objects.create(member=self.member, check_in=at(date(2026, 2, 6)), check_out=at(date(2026, 2, 6), 11))
        assign_to_trainer(self.users["trainer"], self.member, self.live)
        self.login("trainer")

        def ids(url, **params):
            return {v.pk for v in self.client.get(url, params).context["object_list"]}

        hist = reverse("attendance:history")
        self.assertEqual(ids(hist), {a.pk, b.pk, c.pk})
        self.assertEqual(ids(hist, **{"from": "2026-02-01", "to": "2026-02-28"}), {b.pk, c.pk})
        self.assertEqual(ids(hist, **{"to": "2026-02-05"}), {a.pk, b.pk})  # 'to' day is inclusive
        self.assertEqual(ids(hist, q="Live"), {a.pk, b.pk})
        self.assertEqual(ids(hist, q="775550001"), {a.pk, b.pk})
        self.assertEqual(ids(hist, method="qr"), {b.pk})
        mine = reverse("attendance:member_history", args=[self.live.pk])
        self.assertEqual(ids(mine), {a.pk, b.pk})
        self.assertEqual(ids(mine, **{"from": "2026-02-01"}), {b.pk})
        self.assertEqual(self.client.get(mine).context["visit_count"], 2)

    def test_daily_report(self):
        Attendance.objects.create(member=self.live, check_in=at(date(2026, 2, 5), 7), check_out=at(date(2026, 2, 5), 8), method="qr")
        Attendance.objects.create(member=self.member, check_in=at(date(2026, 2, 5), 7, 30))
        Attendance.objects.create(member=self.live, check_in=at(date(2026, 2, 6), 9))
        assign_to_trainer(self.users["trainer"], self.member, self.live)
        self.login("trainer")
        r = self.client.get(reverse("attendance:report"), {"date": "2026-02-05"})
        self.assertEqual((r.context["total"], r.context["unique"], r.context["qr_count"], r.context["still_inside"]), (2, 2, 1, 1))
        self.assertEqual(r.context["average"], "1h 00m")
        seven = next(h for h in r.context["hourly"] if h["hour"] == "07:00")
        self.assertEqual((seven["n"], seven["pct"]), (2, 100))
        self.assertEqual(self.client.get(reverse("attendance:report"), {"date": "garbage"}).status_code, 200)

    def test_pagination(self):
        for i in range(30):
            Attendance.objects.create(member=self.live, check_in=at(date(2026, 1, 1)) + timedelta(days=i), check_out=at(date(2026, 1, 1)) + timedelta(days=i, hours=1))
        assign_to_trainer(self.users["trainer"], self.live)
        self.login("trainer")
        self.assertEqual(len(self.client.get(reverse("attendance:history")).context["object_list"]), 25)
        self.assertEqual(len(self.client.get(reverse("attendance:history"), {"page": 2}).context["object_list"]), 5)

    def test_dashboard_attendance_statistics(self):
        services.check_in(self.live, self.staff)
        stats = get_dashboard_stats()
        self.assertEqual(stats["stats"]["todays_attendance"], 1)
        self.assertEqual(stats["inside_now"], 1)
        self.assertEqual(stats["attendance_chart"]["values"][-1], 1)
        self.assertEqual(sum(stats["attendance_chart"]["values"]), 1)


class PlanAccessRuleFormTests(Base):
    def test_form_validation_and_storage(self):
        self.login("manager")
        base = {"name": "Rules", "description": "", "duration_type": "monthly", "custom_days": "", "price": "1000",
                "benefits": "", "access_restrictions": "", "is_active": "on"}
        r = self.client.post(reverse("memberships:plan_create"), {**base, "access_from": "06:00"})
        self.assertEqual(r.status_code, 200)  # only an opening time
        r = self.client.post(reverse("memberships:plan_create"), {**base, "access_from": "20:00", "access_until": "06:00"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("memberships:plan_create"),
                             {**base, "allowed_weekdays": ["0", "2", "4"], "access_from": "06:00", "access_until": "20:00"})
        self.assertEqual(r.status_code, 302)
        plan = MembershipPlan.objects.get(name="Rules")
        self.assertEqual((plan.allowed_weekdays, plan.access_summary), ("0,2,4", "Mon, Wed, Fri · 06:00–20:00"))
        r = self.client.get(reverse("memberships:plan_edit", args=[plan.pk]))
        self.assertContains(r, 'value="2"')
        all_days = self.client.post(reverse("memberships:plan_create"), {**base, "name": "All", "allowed_weekdays": [str(i) for i in range(7)]})
        self.assertEqual(MembershipPlan.objects.get(name="All").allowed_weekdays, "")

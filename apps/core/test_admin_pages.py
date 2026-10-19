from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.demo import seed_demo
from apps.core.gym import gym
from apps.core.models import ActivityLog, GymSettings
from apps.core.testing import PASSWORD, make_member_with_login, make_users
from apps.members.models import Member
from apps.memberships.models import MembershipPlan
from apps.payments import services as payments


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=self.users[role].username, password=PASSWORD))


class GymSettingsTests(Base):
    form = {"name": "Iron Temple Fitness", "tagline": "Train hard", "address": "12 Galle Road, Colombo", "phone": "0112345678",
            "email": "hello@irontemple.example", "receipt_footer": "Thank you! Fees are non-refundable after 7 days."}

    def test_defaults_come_from_the_environment_until_saved(self):
        self.assertFalse(GymSettings.objects.exists())
        self.assertEqual(gym().name, "IronForge Gym")

    def test_only_authorised_roles_edit_settings(self):
        for role, expected in (("super_admin", 200), ("manager", 200), ("receptionist", 403), ("trainer", 403), ("accountant", 403), ("member", 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("core:gym_settings")).status_code, expected, role)
        self.login("receptionist")
        self.assertEqual(self.client.post(reverse("core:gym_settings"), self.form).status_code, 403)
        self.assertFalse(GymSettings.objects.exists())

    def test_saved_name_and_details_appear_everywhere(self):
        self.login("manager")
        r = self.client.post(reverse("core:gym_settings"), self.form)
        self.assertRedirects(r, reverse("core:gym_settings"))
        saved = GymSettings.objects.get()
        self.assertEqual((saved.pk, saved.name, saved.updated_by), (1, "Iron Temple Fitness", self.users["manager"]))
        self.assertTrue(ActivityLog.objects.filter(action="gym_settings_updated").exists())
        page = self.client.get(reverse("core:home"))
        self.assertContains(page, "Iron Temple Fitness")
        self.assertNotContains(page, "IronForge Gym")
        # receipts (HTML + PDF) carry the contact details and footer
        member, _ = make_member_with_login(self.users, "Receipt Rhea", membership=False)
        from apps.memberships import services as membership_services

        plan = MembershipPlan.objects.create(name="Monthly", price=Decimal("5000"))
        m = membership_services.assign_membership(member, plan, timezone.localdate(), self.users["manager"])
        p, _ = payments.record_payment(m, Decimal("1000"), "cash", self.users["manager"], "gs1")
        receipt = self.client.get(reverse("payments:receipt", args=[p.pk]))
        for text in ("Iron Temple Fitness", "12 Galle Road, Colombo", "0112345678", "Fees are non-refundable"):
            self.assertContains(receipt, text)
        self.assertTrue(self.client.get(reverse("payments:receipt_pdf", args=[p.pk])).content.startswith(b"%PDF"))
        # login page and password-reset e-mail use it too
        self.client.logout()
        self.assertContains(self.client.get(reverse("accounts:login")), "Iron Temple Fitness")
        self.client.post(reverse("accounts:password_reset"), {"email": self.users["manager"].email})
        self.assertIn("Iron Temple Fitness", mail.outbox[-1].subject)

    def test_editing_updates_the_single_row_and_validates(self):
        self.login("manager")
        self.client.post(reverse("core:gym_settings"), self.form)
        self.client.post(reverse("core:gym_settings"), {**self.form, "name": "Iron Temple 2"})
        self.assertEqual((GymSettings.objects.count(), gym().name), (1, "Iron Temple 2"))
        for bad in ({"name": " "}, {"name": "x"}, {"email": "not-an-email"}):
            self.assertEqual(self.client.post(reverse("core:gym_settings"), {**self.form, **bad}).status_code, 200)
        self.assertEqual(gym().name, "Iron Temple 2")


class AuditLogTests(Base):
    def test_access(self):
        for role, expected in (("super_admin", 200), ("manager", 200), ("receptionist", 403), ("trainer", 403), ("accountant", 403), ("member", 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("core:audit_log")).status_code, expected, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core:audit_log")).status_code, 302)

    def test_filters_search_and_pagination(self):
        manager, accountant = self.users["manager"], self.users["accountant"]
        old = ActivityLog.record(manager, "member_created", "Registered member Old Timer")
        ActivityLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))
        ActivityLog.record(accountant, "payment_recorded", "Recorded payment RC-1")
        ActivityLog.record(None, "login_failed", "Failed sign-in for “ghost” from 10.0.0.1 (attempt 1)")
        for i in range(60):
            ActivityLog.record(manager, "expense_recorded", f"Expense {i}")
        self.login("manager")
        url = reverse("core:audit_log")
        r = self.client.get(url)
        self.assertEqual((len(r.context["object_list"]), r.context["page_obj"].paginator.num_pages), (50, 2))
        found = lambda **p: {a.description for a in self.client.get(url, p).context["object_list"]}
        self.assertEqual(found(q="RC-1"), {"Recorded payment RC-1"})
        self.assertEqual(found(user=accountant.pk), {"Recorded payment RC-1"})
        self.assertEqual(found(area="payments"), {"Recorded payment RC-1"})
        self.assertEqual(found(area="members"), {"Registered member Old Timer"})
        self.assertEqual(found(**{"to": (timezone.localdate() - timedelta(days=10)).isoformat()}), {"Registered member Old Timer"})
        self.assertIn("Failed sign-in for “ghost” from 10.0.0.1 (attempt 1)", found(q="ghost"))
        self.assertEqual(len(self.client.get(url, {"page": 2}).context["object_list"]), ActivityLog.objects.count() - 50)

    def test_log_is_read_only_through_the_application(self):
        ActivityLog.record(self.users["manager"], "x", "keep me")
        self.login("super_admin")
        entry = ActivityLog.objects.latest("id")
        for method in ("post", "put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(reverse("core:audit_log")).status_code, 405, method)
        self.assertEqual(self.client.post(f"/audit/{entry.pk}/delete/").status_code, 404)

    def test_important_actions_are_audited_with_who_and_what(self):
        self.login("manager")
        self.client.post(reverse("accounts:user_create"), {"username": "newtrainer", "first_name": "N", "last_name": "T", "email": "nt@example.com", "phone": "",
                                                           "role": "trainer", "is_active": "on", "password1": "Str0ng-Passw0rd!", "password2": "Str0ng-Passw0rd!"})
        self.client.post(reverse("accounts:user_toggle_active", args=[User.objects.get(username="newtrainer").pk]))
        actions = list(ActivityLog.objects.values_list("action", "user__username"))
        self.assertIn(("user_created", "manager"), actions)
        self.assertIn(("user_deactivated", "manager"), actions)
        self.login("super_admin")
        self.client.post(reverse("accounts:permissions"), {"perm_trainer": ["dashboard.view"]})
        self.assertIn(("permissions_updated", "super_admin"), list(ActivityLog.objects.values_list("action", "user__username")))


class HealthAndDemoTests(Base):
    def test_health_endpoint_is_public_minimal_and_reports_database_failures(self):
        from unittest import mock

        r = self.client.get(reverse("core:healthz"))
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))
        with mock.patch("apps.core.views.connection.cursor", side_effect=RuntimeError("db password=hunter2")):
            r = self.client.get(reverse("core:healthz"))
        self.assertEqual((r.status_code, r.json()), (503, {"status": "unavailable"}))
        self.assertNotIn("hunter2", r.content.decode())

    @override_settings(DEBUG=False)
    def test_demo_seeder_refuses_to_run_in_production_mode(self):
        with self.assertRaisesMessage(CommandError, "Refusing"):
            call_command("seed_demo", stdout=StringIO())
        self.assertFalse(Member.objects.exists())


from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.core.models import ActivityLog
from apps.core.stats import get_dashboard_stats

PASSWORD = "Str0ng-Passw0rd!"


class DashboardTests(TestCase):
    def setUp(self):
        cache.clear()
        for role in Role.values:
            User.objects.create_user(username=role, email=f"{role}@example.com", password=PASSWORD, role=role)

    def login(self, role):
        self.assertTrue(self.client.login(username=role, password=PASSWORD))

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("core:home"))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next=/", fetch_redirect_response=False)

    def test_staff_roles_see_dashboard(self):
        for role in ("super_admin", "manager", "receptionist", "trainer", "accountant"):
            self.client.logout()
            self.login(role)
            response = self.client.get(reverse("core:home"))
            self.assertEqual(response.status_code, 200, role)
            self.assertTemplateUsed(response, "core/dashboard.html")

    def test_member_goes_to_portal_not_dashboard(self):
        self.login("member")
        response = self.client.get(reverse("core:home"))
        self.assertRedirects(response, reverse("portal:home"), fetch_redirect_response=False)
        response = self.client.get(reverse("portal:home"))
        self.assertTemplateNotUsed(response, "core/dashboard.html")
        self.assertNotContains(response, "Total members")

    def test_sidebar_hides_modules_the_role_cannot_use(self):
        self.login("trainer")
        response = self.client.get(reverse("core:home"))
        self.assertNotContains(response, reverse("accounts:user_list"))
        self.assertNotContains(response, reverse("accounts:permissions"))
        self.client.logout()
        self.login("super_admin")
        response = self.client.get(reverse("core:home"))
        self.assertContains(response, reverse("accounts:user_list"))
        self.assertContains(response, reverse("accounts:permissions"))

    def test_stats_are_zero_on_an_empty_database(self):
        data = get_dashboard_stats()
        for key, value in data["stats"].items():
            self.assertEqual(value, 0, key)  # real, database-driven zeros – never placeholders
        self.assertEqual(data["upcoming_expirations"], [])

    def test_account_counts_and_role_chart_come_from_database(self):
        User.objects.filter(role="trainer").update(is_active=False)
        data = get_dashboard_stats()
        self.assertEqual(data["user_counts"]["inactive"], 1)
        self.assertEqual(data["user_counts"]["active"], len(Role.values) - 1)
        self.assertNotIn("Trainer", data["role_chart"]["labels"])
        self.assertEqual(sum(data["role_chart"]["values"]), len(Role.values) - 1)

    def test_recent_activities_are_listed(self):
        ActivityLog.record(User.objects.get(username="manager"), "user_created", "Created Trainer account “zed”")
        self.login("super_admin")  # login itself is also logged
        response = self.client.get(reverse("core:home"))
        self.assertContains(response, "Created Trainer account")
        self.assertContains(response, "Signed in")
        self.assertTrue(ActivityLog.objects.first().created_at <= timezone.now() + timedelta(seconds=1))

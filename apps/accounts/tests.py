from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import ActivityLog

from .models import Role, RolePermission, User

PASSWORD = "Str0ng-Passw0rd!"


def make_user(role, username=None, **kwargs):
    username = username or role
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, role=role, **kwargs
    )


class BaseTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_user(Role.SUPER_ADMIN)
        self.manager = make_user(Role.MANAGER)
        self.receptionist = make_user(Role.RECEPTIONIST)
        self.trainer = make_user(Role.TRAINER)
        self.accountant = make_user(Role.ACCOUNTANT)
        self.member = make_user(Role.MEMBER)

    def login(self, user):
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))


class AuthenticationTests(BaseTestCase):
    def test_login_and_logout(self):
        response = self.client.post(reverse("accounts:login"), {"username": "manager", "password": PASSWORD})
        self.assertRedirects(response, reverse("core:home"))
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 200)
        self.assertTrue(ActivityLog.objects.filter(action="login", user=self.manager).exists())
        self.client.post(reverse("accounts:logout"))
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 302)

    def test_logout_requires_post(self):
        self.login(self.manager)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)

    def test_wrong_password_shows_generic_error(self):
        response = self.client.post(reverse("accounts:login"), {"username": "manager", "password": "nope"})
        self.assertContains(response, "Invalid username or password.")

    def test_inactive_account_cannot_login_and_message_is_generic(self):
        self.trainer.is_active = False
        self.trainer.save()
        response = self.client.post(reverse("accounts:login"), {"username": "trainer", "password": PASSWORD})
        self.assertContains(response, "Invalid username or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.client.post(reverse("accounts:login"), {"username": "manager", "password": "bad"})
        response = self.client.post(reverse("accounts:login"), {"username": "manager", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_all_private_pages_redirect_anonymous_users(self):
        urls = [
            reverse("core:home"), reverse("accounts:profile"), reverse("accounts:password_change"),
            reverse("accounts:user_list"), reverse("accounts:user_create"),
            reverse("accounts:user_edit", args=[self.member.pk]), reverse("accounts:permissions"),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith(reverse("accounts:login")), url)
        for url in (reverse("accounts:user_toggle_active", args=[self.member.pk]),):
            self.assertEqual(self.client.post(url).status_code, 302)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_public_pages_are_reachable(self):
        for name in ("accounts:login", "accounts:password_reset", "accounts:password_reset_done"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


class PasswordTests(BaseTestCase):
    def test_password_change(self):
        self.login(self.member)
        new = "An0ther-Str0ng-Pass!"
        response = self.client.post(
            reverse("accounts:password_change"),
            {"old_password": PASSWORD, "new_password1": new, "new_password2": new},
        )
        self.assertRedirects(response, reverse("accounts:profile"))
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(new))
        # Session survives the change.
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 200)

    def test_password_change_rejects_weak_password(self):
        self.login(self.member)
        response = self.client.post(
            reverse("accounts:password_change"),
            {"old_password": PASSWORD, "new_password1": "12345678", "new_password2": "12345678"},
        )
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(PASSWORD))

    def test_password_reset_flow(self):
        response = self.client.post(reverse("accounts:password_reset"), {"email": "member@example.com"})
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        link = next(l for l in mail.outbox[0].body.splitlines() if "/password/reset/" in l).strip()
        path = link.split("://", 1)[1].split("/", 1)[1]
        response = self.client.get("/" + path, follow=True)
        new = "Brand-New-Pass-77!"
        post_url = response.redirect_chain[-1][0]
        response = self.client.post(post_url, {"new_password1": new, "new_password2": new})
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(new))

    def test_password_reset_for_unknown_email_does_not_reveal_it(self):
        response = self.client.post(reverse("accounts:password_reset"), {"email": "ghost@example.com"})
        self.assertRedirects(response, reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)


class UserManagementPermissionTests(BaseTestCase):
    def test_list_access_by_role(self):
        expected = {
            self.admin: 200, self.manager: 200, self.receptionist: 403,
            self.trainer: 403, self.accountant: 403, self.member: 403,
        }
        for user, status in expected.items():
            self.client.logout()
            self.login(user)
            self.assertEqual(self.client.get(reverse("accounts:user_list")).status_code, status, user.role)

    def test_only_super_admin_sees_permission_matrix(self):
        for user in (self.manager, self.receptionist, self.member):
            self.client.logout()
            self.login(user)
            self.assertEqual(self.client.get(reverse("accounts:permissions")).status_code, 403)
            self.assertEqual(self.client.post(reverse("accounts:permissions"), {}).status_code, 403)
        self.client.logout()
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("accounts:permissions")).status_code, 200)

    def user_payload(self, **overrides):
        data = {
            "username": "newstaff", "first_name": "New", "last_name": "Staff", "email": "new@example.com",
            "phone": "+1 555 123 4567", "role": Role.RECEPTIONIST, "is_active": "on",
            "password1": PASSWORD, "password2": PASSWORD,
        }
        data.update(overrides)
        return data

    def test_super_admin_creates_staff_with_any_role(self):
        self.login(self.admin)
        response = self.client.post(reverse("accounts:user_create"), self.user_payload(role=Role.MANAGER))
        self.assertRedirects(response, reverse("accounts:user_list"))
        user = User.objects.get(username="newstaff")
        self.assertEqual(user.role, Role.MANAGER)
        self.assertFalse(user.is_superuser)
        self.assertTrue(ActivityLog.objects.filter(action="user_created").exists())

    def test_manager_cannot_create_privileged_accounts(self):
        self.login(self.manager)
        for role in (Role.SUPER_ADMIN, Role.MANAGER):
            response = self.client.post(reverse("accounts:user_create"), self.user_payload(role=role))
            self.assertEqual(response.status_code, 200)  # re-rendered with validation error
            self.assertFalse(User.objects.filter(username="newstaff").exists())
        response = self.client.post(reverse("accounts:user_create"), self.user_payload(role=Role.TRAINER))
        self.assertRedirects(response, reverse("accounts:user_list"))

    def test_super_admin_role_creates_real_superuser_flags(self):
        self.login(self.admin)
        self.client.post(reverse("accounts:user_create"), self.user_payload(role=Role.SUPER_ADMIN))
        user = User.objects.get(username="newstaff")
        self.assertTrue(user.is_superuser and user.is_staff)

    def test_non_privileged_user_cannot_create_users(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.post(reverse("accounts:user_create"), self.user_payload()).status_code, 403)
        self.assertFalse(User.objects.filter(username="newstaff").exists())

    def test_manager_cannot_edit_or_deactivate_higher_roles(self):
        self.login(self.manager)
        self.assertEqual(self.client.get(reverse("accounts:user_edit", args=[self.admin.pk])).status_code, 403)
        response = self.client.post(reverse("accounts:user_toggle_active", args=[self.admin.pk]))
        self.assertEqual(response.status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_manager_cannot_promote_user_via_edit(self):
        self.login(self.manager)
        response = self.client.post(
            reverse("accounts:user_edit", args=[self.trainer.pk]),
            {"first_name": "T", "last_name": "T", "email": "trainer@example.com", "phone": "",
             "role": Role.SUPER_ADMIN, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.trainer.refresh_from_db()
        self.assertEqual(self.trainer.role, Role.TRAINER)
        self.assertFalse(self.trainer.is_superuser)

    def test_user_cannot_change_own_role_or_status(self):
        self.login(self.manager)
        self.client.post(
            reverse("accounts:user_edit", args=[self.manager.pk]),
            {"first_name": "M", "last_name": "M", "email": "manager@example.com", "phone": "",
             "role": Role.SUPER_ADMIN},
        )
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.role, Role.MANAGER)
        self.assertTrue(self.manager.is_active)
        self.client.post(reverse("accounts:user_toggle_active", args=[self.manager.pk]))
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.is_active)

    def test_activate_and_deactivate(self):
        self.login(self.manager)
        url = reverse("accounts:user_toggle_active", args=[self.trainer.pk])
        self.assertRedirects(self.client.post(url), reverse("accounts:user_list"))
        self.trainer.refresh_from_db()
        self.assertFalse(self.trainer.is_active)
        self.client.post(url)
        self.trainer.refresh_from_db()
        self.assertTrue(self.trainer.is_active)

    def test_toggle_requires_post(self):
        self.login(self.manager)
        self.assertEqual(self.client.get(reverse("accounts:user_toggle_active", args=[self.trainer.pk])).status_code, 405)

    def test_toggle_ignores_external_next_url(self):
        self.login(self.manager)
        response = self.client.post(
            reverse("accounts:user_toggle_active", args=[self.trainer.pk]), {"next": "https://evil.example/"}
        )
        self.assertRedirects(response, reverse("accounts:user_list"))

    def test_deactivated_user_is_logged_out_of_existing_session(self):
        self.login(self.trainer)
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 200)
        self.trainer.is_active = False
        self.trainer.save()
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 302)

    def test_duplicate_email_rejected(self):
        self.login(self.admin)
        response = self.client.post(reverse("accounts:user_create"), self.user_payload(email="MEMBER@example.com"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already in use")

    def test_user_list_search_and_filter(self):
        self.login(self.admin)
        response = self.client.get(reverse("accounts:user_list"), {"q": "trainer"})
        self.assertEqual([u.username for u in response.context["object_list"]], ["trainer"])
        response = self.client.get(reverse("accounts:user_list"), {"role": Role.MEMBER})
        self.assertEqual([u.username for u in response.context["object_list"]], ["member"])

    def test_user_list_paginates(self):
        for i in range(20):
            make_user(Role.MEMBER, username=f"bulk{i}")
        self.login(self.admin)
        response = self.client.get(reverse("accounts:user_list"))
        self.assertEqual(len(response.context["object_list"]), 15)
        self.assertEqual(self.client.get(reverse("accounts:user_list"), {"page": 2}).status_code, 200)


class RolePermissionManagementTests(BaseTestCase):
    def test_defaults_are_seeded(self):
        self.assertTrue(self.manager.has_gym_perm("users.manage"))
        self.assertFalse(self.receptionist.has_gym_perm("users.view"))
        self.assertFalse(self.member.has_gym_perm("dashboard.view"))
        self.assertTrue(self.admin.has_gym_perm("permissions.manage"))
        self.assertFalse(self.manager.has_gym_perm("permissions.manage"))

    def test_super_admin_can_grant_and_revoke(self):
        self.login(self.admin)
        response = self.client.post(
            reverse("accounts:permissions"),
            {"perm_receptionist": ["users.view", "dashboard.view", "bogus.perm", "permissions.manage"]},
        )
        self.assertRedirects(response, reverse("accounts:permissions"))
        granted = set(RolePermission.objects.filter(role="receptionist").values_list("permission", flat=True))
        self.assertEqual(granted, {"users.view", "dashboard.view"})  # unknown / super-admin-only codes ignored
        self.assertFalse(RolePermission.objects.filter(role="manager").exists())

        self.client.logout()
        self.login(self.receptionist)
        self.assertEqual(self.client.get(reverse("accounts:user_list")).status_code, 200)
        # view-only: cannot create
        self.assertEqual(self.client.get(reverse("accounts:user_create")).status_code, 403)

    def test_granting_users_manage_never_unlocks_privileged_roles(self):
        RolePermission.objects.create(role="receptionist", permission="users.manage")
        self.login(self.receptionist)
        response = self.client.post(reverse("accounts:user_edit", args=[self.manager.pk]), {})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(Role.SUPER_ADMIN, self.receptionist.manageable_roles())
        self.assertNotIn(Role.MANAGER, self.receptionist.manageable_roles())


class ProfileTests(BaseTestCase):
    def test_member_can_edit_only_own_profile(self):
        self.login(self.member)
        response = self.client.post(
            reverse("accounts:profile"),
            {"first_name": "Mia", "last_name": "Member", "email": "mia@example.com", "phone": "",
             "role": Role.SUPER_ADMIN, "is_active": ""},
        )
        self.assertRedirects(response, reverse("accounts:profile"))
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, "Mia")
        self.assertEqual(self.member.role, Role.MEMBER)  # role in the payload is ignored
        self.assertFalse(self.member.is_superuser)

    def test_member_cannot_reach_user_management(self):
        self.login(self.member)
        self.assertEqual(self.client.get(reverse("accounts:user_edit", args=[self.admin.pk])).status_code, 403)


@override_settings(DEBUG=False)
class ErrorPageTests(BaseTestCase):
    def test_404_page_is_generic(self):
        self.login(self.admin)
        response = self.client.get("/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Traceback", status_code=404)

    def test_403_page_renders(self):
        self.login(self.member)
        response = self.client.get(reverse("accounts:user_list"))
        self.assertContains(response, "Access denied", status_code=403)

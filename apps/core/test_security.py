"""Security regression tests: direct URL access, CSRF, member isolation, upload handling, error leakage."""
import re
import shutil
import tempfile
from io import BytesIO

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from PIL import Image

from apps.core.demo import DEMO_PASSWORD, seed_demo
from apps.core.testing import PASSWORD, make_users
from apps.members.models import Member

# URLs that are public by design.
PUBLIC = {"/accounts/login/", "/accounts/password/reset/", "/accounts/password/reset/sent/", "/accounts/password/reset/complete/", "/healthz/"}
# Pages every signed-in user (including members) may open.
EVERYONE = ("/", "/accounts/profile/", "/accounts/password/change/", "/notifications/", "/healthz/")
CONVERTERS = {"int": "1", "slug": "expenses", "str": "x"}


def all_paths():
    """Every URL pattern as a concrete path (ids replaced by 1, report slug by a real report)."""
    found = []

    def walk(patterns, prefix):
        for p in patterns:
            route = str(p.pattern)
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + route)
            elif isinstance(p, URLPattern):
                path = "/" + (prefix + route).lstrip("/")
                if "uidb64" in path or path.startswith("/django-admin"):
                    continue
                path = re.sub(r"<(\w+):(\w+)>", lambda m: CONVERTERS[m.group(1)], path)
                found.append(path)

    walk(get_resolver().url_patterns, "")
    return sorted(set(found))


class SecurityBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.users = make_users()
        cls.demo = seed_demo(cls.users["manager"])

    def login(self, username, password=PASSWORD, client=None):
        client = client or self.client
        client.logout()
        self.assertTrue(client.login(username=username, password=password))
        return client


class DirectUrlAccessTests(SecurityBase):
    def test_discovery_finds_the_whole_site(self):
        paths = all_paths()
        self.assertGreater(len(paths), 140)
        for expected in ("/members/", "/payments/1/refund/", "/reports/expenses/", "/expenses/1/receipt/", "/trainers/certificates/1/download/"):
            self.assertIn(expected, paths)

    def test_anonymous_users_can_reach_nothing_private(self):
        offenders = []
        for path in all_paths():
            if path in PUBLIC or path == "/notifications/webhooks/twilio/":
                continue
            for method in ("get", "post"):
                r = getattr(self.client, method)(path)
                if r.status_code not in (302, 301) or "/accounts/login/" not in r["Location"]:
                    offenders.append((method, path, r.status_code))
        self.assertEqual(offenders, [])

    def test_public_pages_expose_nothing_sensitive(self):
        r = self.client.get("/healthz/")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))
        self.assertEqual(self.client.get("/notifications/webhooks/twilio/").status_code, 405)
        self.assertEqual(self.client.post("/notifications/webhooks/twilio/", {"MessageSid": "x"}).status_code, 404)  # no token configured

    def test_members_can_only_open_their_own_area(self):
        self.login("demo_alice", DEMO_PASSWORD)
        offenders = []
        for path in all_paths():
            r = self.client.get(path)
            allowed = path.startswith("/me/") or path in EVERYONE or path in PUBLIC or path.startswith("/notifications/") and "webhooks" not in path
            if r.status_code == 200 and not allowed:
                offenders.append((path, r.status_code))
            if r.status_code >= 500:
                offenders.append((path, r.status_code))
        self.assertEqual(offenders, [])

    def test_members_cannot_write_to_staff_endpoints(self):
        self.login("demo_alice", DEMO_PASSWORD)
        before = (Member.objects.count(), self.demo["plan"].items.count())
        offenders = []
        for path in all_paths():
            if path.startswith(("/me/", "/accounts/", "/notifications/", "/healthz/")) or path == "/":
                continue
            r = self.client.post(path, {"anything": "x", "reason": "no", "status": "archived"})
            if r.status_code in (200, 201, 204, 302):
                offenders.append((path, r.status_code))
        self.assertEqual(offenders, [])
        self.assertEqual((Member.objects.count(), self.demo["plan"].items.count()), before)

    def test_each_staff_role_is_limited_to_its_modules(self):
        restricted = {
            "receptionist": ["/accounts/users/", "/accounts/permissions/", "/audit/", "/settings/gym/", "/expenses/", "/reports/", "/notifications/settings/", "/announcements/",
                             "/trainers/new/", "/payments/1/refund/", "/workouts/plans/", "/progress/"],
            "trainer": ["/accounts/users/", "/audit/", "/settings/gym/", "/expenses/", "/payments/", "/reports/", "/inventory/", "/announcements/", "/trainers/new/",
                        "/attendance/1/check-out/", "/payments/outstanding/", "/plans/new/"],
            "accountant": ["/accounts/users/", "/audit/", "/settings/gym/", "/attendance/", "/appointments/", "/workouts/plans/", "/announcements/", "/trainers/new/", "/members/new/"],
        }
        offenders = []
        for role, paths in restricted.items():
            self.login(role)
            for path in paths:
                r = self.client.get(path)
                if r.status_code == 200:
                    offenders.append((role, path))
        self.assertEqual(offenders, [])

    def test_only_the_manager_and_super_admin_see_audit_and_settings(self):
        for role, expected in (("super_admin", 200), ("manager", 200), ("receptionist", 403), ("trainer", 403), ("accountant", 403), ("member", 403)):
            self.login(role)
            self.assertEqual(self.client.get("/audit/").status_code, expected, role)
            self.assertEqual(self.client.get("/settings/gym/").status_code, expected, role)

    def test_data_isolation_between_members(self):
        alice_plan, bob = self.demo["plan"], self.demo["bob"]
        self.login("demo_bob", DEMO_PASSWORD)
        self.assertEqual(self.client.get(f"/me/workouts/{alice_plan.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/me/appointments/{self.demo['appointment'].pk}/reschedule/").status_code, 404)
        self.assertNotContains(self.client.get("/me/"), "Demo Alice")
        self.assertNotContains(self.client.get("/me/workouts/"), "Demo Strength Plan")
        self.assertNotContains(self.client.get("/me/progress/"), "Demo start")


class CsrfTests(SecurityBase):
    def test_state_changing_requests_without_a_token_are_rejected(self):
        client = Client(enforce_csrf_checks=True)
        self.login("manager", client=client)
        payment = self.demo["membership"].payments.first()
        targets = [("/accounts/logout/", {}), (f"/payments/{payment.pk}/refund/", {"amount": "1", "method": "cash", "reason": "x y z", "idempotency_key": "abc"}),
                   ("/members/1/toggle-active/", {}), ("/expenses/new/", {}), ("/notifications/read/", {}), ("/accounts/permissions/", {})]
        for path, data in targets:
            self.assertEqual(client.post(path, data).status_code, 403, path)
        self.assertTrue(Member.objects.get(pk=1).is_active)

    def test_login_form_is_csrf_protected_and_logout_needs_post(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/accounts/login/", {"username": "manager", "password": PASSWORD}).status_code, 403)
        self.login("manager")
        self.assertEqual(self.client.get("/accounts/logout/").status_code, 405)
        self.assertEqual(self.client.get("/").status_code, 200)  # a GET cannot sign anybody out


class UploadSecurityTests(SecurityBase):
    def setUp(self):
        self.tmp_private, self.tmp_media = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_private, True)
        self.addCleanup(shutil.rmtree, self.tmp_media, True)
        self.settings_override = override_settings(PRIVATE_MEDIA_ROOT=self.tmp_private, MEDIA_ROOT=self.tmp_media)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def png(self, name="a.png", extra=b""):
        buf = BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, "PNG")
        return SimpleUploadedFile(name, buf.getvalue() + extra, content_type="image/png")

    def member_payload(self, photo):
        return {"full_name": "Upload Test", "phone": "0770009999", "email": "", "date_of_birth": "1990-01-01", "address": "", "emergency_contact_name": "K",
                "emergency_contact_phone": "0710009999", "registration_date": timezone.localdate().isoformat(), "fitness_goals": "", "photo": photo}

    def test_member_photos_reject_scripts_svg_and_mislabelled_files(self):
        self.login("receptionist")
        bad = [SimpleUploadedFile("evil.php", b"<?php system($_GET['c']); ?>", content_type="image/png"),
               SimpleUploadedFile("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', content_type="image/svg+xml"),
               SimpleUploadedFile("shell.png", b"GIF89a<?php echo 1; ?>", content_type="image/png"),
               SimpleUploadedFile("doc.pdf", b"%PDF-1.4 fake", content_type="application/pdf")]
        for upload in bad:
            self.assertEqual(self.client.post("/members/new/", self.member_payload(upload)).status_code, 200, upload.name)
        self.assertFalse(Member.objects.filter(phone="0770009999").exists())

    def test_stored_names_are_random_and_cannot_traverse_directories(self):
        self.login("receptionist")
        self.client.post("/members/new/", self.member_payload(self.png("../../../etc/passwd.php.png")))
        member = Member.objects.get(phone="0770009999")
        name = member.photo.name
        self.assertRegex(name, r"^members/photos/[0-9a-f]{32}\.png$")
        self.assertTrue(member.photo.path.startswith(self.tmp_media))

    def test_documents_are_validated_by_content_and_kept_private(self):
        self.login("accountant")
        from apps.expenses.models import ExpenseCategory

        category = ExpenseCategory.objects.first().pk
        def post(upload):
            return self.client.post("/expenses/new/", {"category": category, "amount": "10", "expense_date": timezone.localdate().isoformat(), "description": "Upload check", "receipt": upload})

        for upload in (SimpleUploadedFile("r.pdf", b"not really a pdf"), SimpleUploadedFile("r.html", b"<script>alert(1)</script>"),
                       SimpleUploadedFile("r.png", b"%PDF-1.4"), SimpleUploadedFile("r.exe", b"MZ")):
            self.assertEqual(post(upload).status_code, 200, upload.name)
        r = post(SimpleUploadedFile("receipt.pdf", b"%PDF-1.4\n%%EOF"))
        self.assertEqual(r.status_code, 302)
        from apps.expenses.models import Expense

        expense = Expense.objects.get(description="Upload check")
        self.assertTrue(expense.receipt.path.startswith(self.tmp_private))  # not under MEDIA_ROOT, so never served by URL
        self.assertFalse(expense.receipt.path.startswith(self.tmp_media))


class ErrorHandlingTests(SecurityBase):
    def test_server_errors_do_not_leak_details(self):
        from unittest import mock

        client = Client(raise_request_exception=False)
        self.login("manager", client=client)
        with mock.patch("apps.members.views.MemberListView.get_queryset", side_effect=RuntimeError("SECRET-DB-PASSWORD=hunter2 /srv/app/secret.py")):
            r = client.get("/members/")
        self.assertEqual(r.status_code, 500)
        body = r.content.decode()
        self.assertNotIn("hunter2", body)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("RuntimeError", body)
        self.assertIn("Something went wrong", body)

    def test_not_found_and_forbidden_pages_are_generic(self):
        self.login("receptionist")
        for path, status in (("/expenses/", 403), ("/members/99999/", 404)):
            r = self.client.get(path)
            self.assertEqual(r.status_code, status)
            self.assertNotIn("Traceback", r.content.decode())

    def test_security_headers_and_cookies(self):
        client = Client()
        signed_in = client.post("/accounts/login/", {"username": "manager", "password": PASSWORD})  # a real sign-in, so we see the real Set-Cookie
        self.assertEqual(signed_in.status_code, 302)
        cookie = signed_in.cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        r = client.get("/")
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["Referrer-Policy"], "same-origin")


class PasswordAndSessionTests(SecurityBase):
    def test_passwords_are_validated_and_hashed(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        for weak in ("short", "1234567890", "password123", "gymsystem"):
            with self.assertRaises(ValidationError):
                validate_password(weak)
        validate_password("a-Long&Unusual-passphrase-91")
        manager = self.users["manager"]
        self.assertNotIn(PASSWORD, manager.password)
        self.assertIn("$", manager.password)  # algorithm$salt$hash

    def test_deactivated_accounts_lose_access_immediately(self):
        self.login("receptionist")
        self.assertEqual(self.client.get("/members/").status_code, 200)
        self.users["receptionist"].is_active = False
        self.users["receptionist"].save()
        self.assertEqual(self.client.get("/members/").status_code, 302)

    def test_failed_logins_are_audited_without_passwords_and_lockout_is_logged(self):
        from apps.core.models import ActivityLog

        for _ in range(5):
            self.client.post("/accounts/login/", {"username": "manager", "password": "wrong-Password-1"})
        entries = list(ActivityLog.objects.filter(action__in=["login_failed", "login_locked"]).values_list("action", "description"))
        self.assertEqual([a for a, _ in entries].count("login_failed"), 5)
        self.assertEqual([a for a, _ in entries].count("login_locked"), 1)
        self.assertFalse(any("wrong-Password-1" in d for _, d in entries))
        self.assertEqual(self.client.post("/accounts/login/", {"username": "manager", "password": PASSWORD}).status_code, 429)

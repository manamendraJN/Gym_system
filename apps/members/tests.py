import shutil
import tempfile
from datetime import date, timedelta
from io import BytesIO

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import Role, User
from apps.memberships import services
from apps.memberships.models import Membership, MembershipPlan

from .models import Member

PASSWORD = "Str0ng-Passw0rd!"
TMP_MEDIA = tempfile.mkdtemp()


def make_user(role):
    return User.objects.create_user(username=role, email=f"{role}@example.com", password=PASSWORD, role=role)


def make_member(**kw):
    n = Member.objects.count() + 1
    data = dict(full_name=f"Member {n}", phone=f"077{n:07d}", date_of_birth=date(1995, 5, 5),
                emergency_contact_name="Parent", emergency_contact_phone=f"071{n:07d}",
                registration_date=timezone.localdate())
    data.update(kw)
    return Member.objects.create(**data)


def png(size=(20, 20), name="p.png"):
    buf = BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def payload(**kw):
    data = {
        "full_name": "Nimal Perera", "phone": "077 123 4567", "email": "Nimal@Example.com",
        "date_of_birth": "1990-03-04", "address": "Colombo", "emergency_contact_name": "Kamala",
        "emergency_contact_phone": "0712223333", "registration_date": timezone.localdate().isoformat(),
        "fitness_goals": "Lose weight",
    }
    data.update(kw)
    return data


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class MemberTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.users = {r: make_user(r) for r in Role.values}

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))


class MemberRegistrationTests(MemberTestCase):
    def test_register_member_generates_unique_id_and_normalises_data(self):
        self.login("receptionist")
        response = self.client.post(reverse("members:create"), payload())
        m = Member.objects.get()
        self.assertRedirects(response, m.get_absolute_url())
        self.assertEqual(m.member_id, f"GM{m.pk:06d}")
        self.assertEqual(m.phone, "0771234567")
        self.assertEqual(m.email, "nimal@example.com")
        self.client.post(reverse("members:create"), payload(full_name="Second", phone="0779999999", email="b@example.com"))
        ids = list(Member.objects.values_list("member_id", flat=True))
        self.assertEqual(len(set(ids)), 2)

    def test_duplicate_phone_in_any_format_is_rejected(self):
        self.login("receptionist")
        self.client.post(reverse("members:create"), payload())
        for phone in ("0771234567", "+94771234567", "077-123-4567"):
            response = self.client.post(reverse("members:create"), payload(phone=phone, email="other@example.com"))
            self.assertEqual(response.status_code, 200, phone)
            self.assertContains(response, "already registered")
        self.assertEqual(Member.objects.count(), 1)

    def test_duplicate_email_case_insensitive(self):
        self.login("receptionist")
        self.client.post(reverse("members:create"), payload())
        response = self.client.post(reverse("members:create"), payload(phone="0770000001", email="NIMAL@example.COM"))
        self.assertContains(response, "already registered")
        self.assertEqual(Member.objects.count(), 1)

    def test_blank_emails_do_not_clash(self):
        self.login("receptionist")
        self.client.post(reverse("members:create"), payload(email=""))
        self.client.post(reverse("members:create"), payload(email="", phone="0770000002", full_name="Another"))
        self.assertEqual(Member.objects.count(), 2)

    def test_database_enforces_uniqueness(self):
        make_member(phone="0771111111", email="x@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_member(phone="0771111111")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_member(phone="0772222222", email="X@example.com")

    def test_validation_errors(self):
        self.login("receptionist")
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        cases = {
            "date_of_birth": tomorrow, "registration_date": tomorrow, "phone": "123", "full_name": " ",
            "emergency_contact_phone": "0771234567",  # same as member's
        }
        for field, value in cases.items():
            response = self.client.post(reverse("members:create"), payload(**{field: value}))
            self.assertEqual(response.status_code, 200, field)
            self.assertTrue(response.context["form"].errors, field)
        self.assertEqual(Member.objects.count(), 0)

    def test_photo_upload_and_validation(self):
        self.login("receptionist")
        self.client.post(reverse("members:create"), {**payload(), "photo": png()})
        m = Member.objects.get()
        self.assertTrue(m.photo.name.startswith("members/photos/"))
        bad = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        response = self.client.post(reverse("members:create"), {**payload(phone="0770000003", email=""), "photo": bad})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Member.objects.count(), 1)

    def test_oversized_photo_rejected(self):
        self.login("receptionist")
        big = png()
        big = SimpleUploadedFile("big.png", big.read() + bytes(3 * 1024 * 1024), content_type="image/png")
        response = self.client.post(reverse("members:create"), {**payload(), "photo": big})
        self.assertContains(response, "2 MB")
        self.assertEqual(Member.objects.count(), 0)

    def test_edit_member_and_old_photo_removed_on_replace(self):
        self.login("receptionist")
        self.client.post(reverse("members:create"), {**payload(), "photo": png(name="a.png")})
        m = Member.objects.get()
        old = m.photo.name
        response = self.client.post(reverse("members:edit", args=[m.pk]),
                                    {**payload(full_name="Nimal P. Perera"), "photo": png(name="b.png")})
        self.assertRedirects(response, m.get_absolute_url())
        m.refresh_from_db()
        self.assertEqual(m.full_name, "Nimal P. Perera")
        self.assertNotEqual(m.photo.name, old)
        self.assertFalse(m.photo.storage.exists(old))

    def test_editing_without_changes_does_not_trip_uniqueness(self):
        self.login("receptionist")
        self.client.post(reverse("members:create"), payload())
        m = Member.objects.get()
        response = self.client.post(reverse("members:edit", args=[m.pk]), payload())
        self.assertRedirects(response, m.get_absolute_url())


class MemberPermissionTests(MemberTestCase):
    def test_access_matrix(self):
        m = make_member()
        expectations = {  # role: (list, create, detail, edit)
            "super_admin": (200, 200, 200, 200), "manager": (200, 200, 200, 200),
            "receptionist": (200, 200, 200, 200), "trainer": (200, 403, 404, 403),  # trainers only open assigned members
            "accountant": (200, 403, 200, 403), "member": (403, 403, 403, 403),
        }
        for role, (lst, create, detail, edit) in expectations.items():
            self.login(role)
            self.assertEqual(self.client.get(reverse("members:list")).status_code, lst, role)
            self.assertEqual(self.client.get(reverse("members:create")).status_code, create, role)
            self.assertEqual(self.client.get(reverse("members:detail", args=[m.pk])).status_code, detail, role)
            self.assertEqual(self.client.get(reverse("members:edit", args=[m.pk])).status_code, edit, role)

    def test_trainer_cannot_post_or_deactivate(self):
        m = make_member()
        self.login("trainer")
        self.assertEqual(self.client.post(reverse("members:create"), payload()).status_code, 403)
        self.assertEqual(self.client.post(reverse("members:toggle_active", args=[m.pk])).status_code, 403)
        m.refresh_from_db()
        self.assertTrue(m.is_active)

    def test_anonymous_is_redirected(self):
        m = make_member()
        for url in (reverse("members:list"), reverse("members:detail", args=[m.pk]), reverse("members:create")):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_trainer_without_membership_permission_still_sees_profile_but_not_billing_actions(self):
        m = make_member()
        from apps.core.testing import make_trainer
        from apps.trainers import services as trainer_services

        trainer_services.assign_trainer(make_trainer(user=self.users["trainer"]), m, self.users["manager"])
        self.login("trainer")
        response = self.client.get(reverse("members:detail", args=[m.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("memberships:assign", args=[m.pk]))
        self.assertNotContains(response, reverse("members:edit", args=[m.pk]))


class MemberListTests(MemberTestCase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.a = make_member(full_name="Anna Silva", phone="0771000001", email="anna@example.com",
                             registration_date=today - timedelta(days=40))
        self.b = make_member(full_name="Bruno Fernando", phone="0772000002", email="bruno@example.com",
                             registration_date=today - timedelta(days=10))
        self.c = make_member(full_name="Chandra Perera", phone="0773000003", registration_date=today)
        self.login("receptionist")

    def names(self, **params):
        response = self.client.get(reverse("members:list"), params)
        return [m.full_name for m in response.context["object_list"]]

    def test_search_by_name_id_phone_email(self):
        self.assertEqual(self.names(q="bruno"), ["Bruno Fernando"])
        self.assertEqual(self.names(q=self.c.member_id), ["Chandra Perera"])
        self.assertEqual(self.names(q="0772000"), ["Bruno Fernando"])
        self.assertEqual(self.names(q="anna@example"), ["Anna Silva"])
        self.assertEqual(self.names(q="nobody"), [])

    def test_filter_by_registration_date(self):
        today = timezone.localdate()
        self.assertEqual(self.names(**{"from": (today - timedelta(days=15)).isoformat()}), ["Bruno Fernando", "Chandra Perera"])
        self.assertEqual(self.names(to=(today - timedelta(days=15)).isoformat()), ["Anna Silva"])

    def test_filter_by_status(self):
        plan = MembershipPlan.objects.create(name="Monthly", price=5000)
        services.assign_membership(self.a, plan, timezone.localdate(), self.users["manager"])
        self.b.is_active = False
        self.b.save()
        self.assertEqual(self.names(status="active"), ["Anna Silva"])
        self.assertEqual(self.names(status="inactive"), ["Bruno Fernando"])
        self.assertEqual(self.names(status="none"), ["Chandra Perera"])

    def test_sorting(self):
        self.assertEqual(self.names(sort="registered", dir="desc"), ["Chandra Perera", "Bruno Fernando", "Anna Silva"])
        self.assertEqual(self.names(sort="name", dir="desc"), ["Chandra Perera", "Bruno Fernando", "Anna Silva"])
        self.assertEqual(self.names(sort="evil; drop table", dir="x"), ["Anna Silva", "Bruno Fernando", "Chandra Perera"])

    def test_pagination(self):
        for i in range(20):
            make_member(full_name=f"Zed {i:02d}")
        response = self.client.get(reverse("members:list"))
        self.assertEqual(len(response.context["object_list"]), 15)
        response = self.client.get(reverse("members:list"), {"page": 2, "q": "Zed"})
        self.assertEqual(len(response.context["object_list"]), 5)


class MemberDeactivationTests(MemberTestCase):
    def test_deactivate_keeps_history_and_blocks_new_assignments(self):
        m = make_member()
        plan = MembershipPlan.objects.create(name="Monthly", price=5000)
        ms = services.assign_membership(m, plan, timezone.localdate(), self.users["manager"])
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("members:toggle_active", args=[m.pk])).status_code, 405)
        self.client.post(reverse("members:toggle_active", args=[m.pk]))
        m.refresh_from_db()
        self.assertFalse(m.is_active)
        self.assertTrue(Membership.objects.filter(pk=ms.pk).exists())
        self.assertContains(self.client.get(m.get_absolute_url()), "deactivated")
        self.client.post(reverse("members:toggle_active", args=[m.pk]))
        m.refresh_from_db()
        self.assertTrue(m.is_active)

    def test_members_with_memberships_cannot_be_deleted(self):
        m = make_member()
        plan = MembershipPlan.objects.create(name="Monthly", price=5000)
        services.assign_membership(m, plan, timezone.localdate(), self.users["manager"])
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            m.delete()

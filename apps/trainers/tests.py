import shutil
import tempfile
from datetime import date, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.appointments import services as appointment_services
from apps.appointments.models import Appointment
from apps.core.models import Notification
from apps.core.testing import PASSWORD, future_slot, give_membership, make_trainer, make_users
from apps.members.tests import make_member

from . import services
from .access import scope_members
from .models import (
    EmploymentStatus, Trainer, TrainerAssignment, TrainerAvailability, TrainerCertificate, TrainerCompensation,
)
from .services import AssignmentError

PRIVATE = tempfile.mkdtemp()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.staff = self.users["manager"]
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"])
        self.member = make_member(full_name="Alice Member")

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))


class TrainerProfileTests(Base):
    def form(self, **kw):
        data = {"full_name": "Coach Lee", "phone": "0771112222", "email": "lee@example.com", "specialties": "Yoga, Rehab",
                "qualifications": "Level 3", "bio": "", "employment_status": "active", "hire_date": timezone.localdate().isoformat(),
                "hourly_rate": "2500.00"}
        data.update(kw)
        return data

    def test_manager_creates_and_edits_trainer(self):
        self.login("manager")
        r = self.client.post(reverse("trainers:create"), self.form())
        t = Trainer.objects.get(full_name="Coach Lee")
        self.assertRedirects(r, t.get_absolute_url())
        self.assertEqual((t.specialty_list, t.hourly_rate), (["Yoga", "Rehab"], Decimal("2500.00")))
        self.client.post(reverse("trainers:edit", args=[t.pk]), self.form(hourly_rate="3000", full_name="Coach Lee Jr"))
        t.refresh_from_db()
        self.assertEqual((t.full_name, t.hourly_rate), ("Coach Lee Jr", Decimal("3000.00")))

    def test_validation(self):
        self.login("manager")
        for bad in (self.form(phone="12"), self.form(hourly_rate="-1"), self.form(full_name=" ")):
            self.assertEqual(self.client.post(reverse("trainers:create"), bad).status_code, 200)
        self.assertEqual(Trainer.objects.count(), 1)

    def test_login_link_options_are_limited_to_free_trainer_accounts(self):
        self.login("manager")
        form = self.client.get(reverse("trainers:create")).context["form"]
        usernames = set(form.fields["user"].queryset.values_list("username", flat=True))
        self.assertNotIn("trainer", usernames)  # already attached to Coach Kim
        self.assertNotIn("manager", usernames)  # not a trainer-role account

    def test_permissions(self):
        for role, view, manage in (("manager", 200, 200), ("trainer", 200, 403), ("receptionist", 200, 403), ("accountant", 200, 403), ("member", 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("trainers:list")).status_code, view, role)
            self.assertEqual(self.client.get(reverse("trainers:detail", args=[self.trainer.pk])).status_code, view, role)
            self.assertEqual(self.client.get(reverse("trainers:create")).status_code, manage, role)
            self.assertEqual(self.client.get(reverse("trainers:edit", args=[self.trainer.pk])).status_code, manage, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("trainers:list")).status_code, 302)

    def test_list_search_and_filter(self):
        make_trainer("Zed Yogi", specialties="Yoga", employment_status=EmploymentStatus.ON_LEAVE)
        self.login("manager")
        names = lambda **p: {t.full_name for t in self.client.get(reverse("trainers:list"), p).context["object_list"]}
        self.assertEqual(names(q="yoga"), {"Zed Yogi"})
        self.assertEqual(names(status="active"), {"Coach Kim"})
        self.assertEqual(names(), {"Coach Kim", "Zed Yogi"})


class CertificateAndCompensationTests(Base):
    def upload(self, name="cert.pdf", content=PDF):
        return self.client.post(reverse("trainers:certificate_add", args=[self.trainer.pk]),
                                {"title": "Level 3 PT", "issuer": "REPS", "file": SimpleUploadedFile(name, content)})

    def test_certificate_upload_is_private_and_validated(self):
        self.login("manager")
        self.assertEqual(self.upload().status_code, 302)
        cert = TrainerCertificate.objects.get()
        self.assertTrue(cert.file.path.startswith(PRIVATE))  # outside MEDIA_ROOT, never served by URL
        with self.assertRaises(ValueError):
            cert.file.url
        self.assertEqual(self.upload("fake.pdf", b"not a pdf").status_code, 200)
        self.assertEqual(self.upload("evil.exe", b"MZ").status_code, 200)
        self.assertEqual(self.upload("big.pdf", PDF + b"0" * (6 * 1024 * 1024)).status_code, 200)
        self.assertEqual(TrainerCertificate.objects.count(), 1)

    def test_certificate_download_needs_permission_and_is_not_cached(self):
        self.login("manager")
        self.upload()
        cert = TrainerCertificate.objects.get()
        url = reverse("trainers:certificate_download", args=[cert.pk])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), PDF)
        self.assertIn("no-store", r["Cache-Control"])
        for role in ("trainer", "receptionist", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_certificate_delete_removes_file(self):
        self.login("manager")
        self.upload()
        cert = TrainerCertificate.objects.get()
        path = cert.file.path
        self.client.post(reverse("trainers:certificate_delete", args=[cert.pk]))
        import os

        self.assertFalse(os.path.exists(path))

    def comp(self, **kw):
        data = {"kind": "salary", "amount": "120000", "period_start": "2026-01-01", "period_end": "2026-01-31", "notes": ""}
        data.update(kw)
        return self.client.post(reverse("trainers:compensation_add", args=[self.trainer.pk]), data)

    def test_compensation_restricted_and_append_only(self):
        for role in ("receptionist", "trainer", "member"):
            self.login(role)
            self.assertEqual(self.comp().status_code, 403, role)
        self.login("accountant")
        self.assertEqual(self.comp().status_code, 302)
        self.assertEqual(self.comp(amount="0").status_code, 200)
        self.assertEqual(self.comp(period_end="2025-12-01").status_code, 200)
        record = TrainerCompensation.objects.get()
        self.assertEqual((record.amount, record.recorded_by), (Decimal("120000.00"), self.users["accountant"]))
        with self.assertRaises(ValueError):
            record.delete()
        # visibility on the profile page
        self.assertContains(self.client.get(self.trainer.get_absolute_url()), "Salary")
        for role in ("trainer", "receptionist"):
            self.login(role)
            self.assertNotContains(self.client.get(self.trainer.get_absolute_url()), "Salary")
        self.login("manager")
        self.assertContains(self.client.get(self.trainer.get_absolute_url()), "Salary")


class AssignmentTests(Base):
    def test_assign_and_history(self):
        a = services.assign_trainer(self.trainer, self.member, self.staff, "Strength focus")
        self.assertEqual((a.end_date, a.assigned_by), (None, self.staff))
        with self.assertRaisesMessage(AssignmentError, "already assigned"):
            services.assign_trainer(self.trainer, self.member, self.staff)
        services.end_assignment(a, self.staff)
        a.refresh_from_db()
        self.assertEqual((a.end_date, a.ended_by), (timezone.localdate(), self.staff))
        with self.assertRaisesMessage(AssignmentError, "already ended"):
            services.end_assignment(a, self.staff)
        services.assign_trainer(self.trainer, self.member, self.staff)  # can be re-assigned later
        self.assertEqual(TrainerAssignment.objects.filter(member=self.member).count(), 2)  # history is kept

    def test_rules(self):
        gone = make_trainer("Gone", employment_status=EmploymentStatus.TERMINATED)
        with self.assertRaisesMessage(AssignmentError, "left the gym"):
            services.assign_trainer(gone, self.member, self.staff)
        self.member.is_active = False
        self.member.save()
        with self.assertRaisesMessage(AssignmentError, "deactivated"):
            services.assign_trainer(self.trainer, self.member, self.staff)

    def test_database_allows_one_open_assignment_per_pair(self):
        TrainerAssignment.objects.create(trainer=self.trainer, member=self.member)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrainerAssignment.objects.create(trainer=self.trainer, member=self.member)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrainerAssignment.objects.create(trainer=make_trainer("Other"), member=self.member, start_date=date(2026, 5, 1), end_date=date(2026, 4, 1))

    def test_a_member_can_have_several_trainers(self):
        services.assign_trainer(self.trainer, self.member, self.staff)
        services.assign_trainer(make_trainer("Second"), self.member, self.staff)
        self.assertEqual(TrainerAssignment.objects.filter(member=self.member, end_date=None).count(), 2)

    def test_assign_view_and_permissions(self):
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("trainers:assign")).status_code, 403)
        self.login("manager")
        r = self.client.post(reverse("trainers:assign"), {"trainer": self.trainer.pk, "member": self.member.member_id.lower()})
        self.assertRedirects(r, self.trainer.get_absolute_url())
        r = self.client.post(reverse("trainers:assign"), {"trainer": self.trainer.pk, "member": self.member.member_id})
        self.assertContains(r, "already assigned")
        r = self.client.post(reverse("trainers:assign"), {"trainer": self.trainer.pk, "member": "GM999999"})
        self.assertContains(r, "No member found")
        assignment = TrainerAssignment.objects.get()
        self.assertEqual(self.client.post(reverse("trainers:end_assignment", args=[assignment.pk])).status_code, 302)
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.end_date)
        self.assertEqual(self.client.get(reverse("trainers:end_assignment", args=[assignment.pk])).status_code, 405)

    def test_assignment_notifies_trainer_and_member(self):
        from apps.core.testing import make_member_with_login

        member, login = make_member_with_login(self.users, "Bella")
        services.assign_trainer(self.trainer, member, self.staff)
        self.assertEqual(Notification.objects.filter(user=self.users["trainer"]).count(), 1)
        self.assertEqual(Notification.objects.filter(user=login, title="Trainer assigned").count(), 1)

    def test_trainer_roster_and_member_scope(self):
        other = make_member(full_name="Not Mine")
        services.assign_trainer(self.trainer, self.member, self.staff)
        mine = set(scope_members(self.users["trainer"], type(self.member).objects.all()).values_list("pk", flat=True))
        self.assertEqual(mine, {self.member.pk})
        self.login("trainer")
        r = self.client.get(reverse("trainers:my_members"))
        self.assertEqual([m.pk for m in r.context["object_list"]], [self.member.pk])
        self.assertEqual(self.client.get(self.member.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(other.get_absolute_url()).status_code, 404)
        self.assertEqual({m.pk for m in self.client.get(reverse("members:list")).context["object_list"]}, {self.member.pk})
        # other staff are not limited
        self.login("receptionist")
        self.assertEqual(self.client.get(other.get_absolute_url()).status_code, 200)

    def test_ending_an_assignment_removes_trainer_access_immediately(self):
        a = services.assign_trainer(self.trainer, self.member, self.staff, start_date=timezone.localdate() - timedelta(days=5))
        self.login("trainer")
        self.assertEqual(self.client.get(self.member.get_absolute_url()).status_code, 200)
        services.end_assignment(a, self.staff)
        self.assertEqual(self.client.get(self.member.get_absolute_url()).status_code, 404)


class AvailabilityTests(Base):
    def test_availability_validation_and_deletion(self):
        trainer = make_trainer("Fresh", availability=False)
        self.login("manager")
        url = reverse("trainers:availability_add", args=[trainer.pk])
        self.assertEqual(self.client.post(url, {"weekday": 1, "start_time": "09:00", "end_time": "12:00"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"weekday": 1, "start_time": "11:00", "end_time": "14:00"}).status_code, 200)  # overlaps
        self.assertEqual(self.client.post(url, {"weekday": 1, "start_time": "12:00", "end_time": "14:00"}).status_code, 302)  # touching is fine
        self.assertEqual(self.client.post(url, {"weekday": 2, "start_time": "15:00", "end_time": "14:00"}).status_code, 200)
        self.assertEqual(trainer.availability.count(), 2)
        window = trainer.availability.first()
        self.client.post(reverse("trainers:availability_delete", args=[window.pk]))
        self.assertEqual(trainer.availability.count(), 1)
        for role in ("trainer", "receptionist"):
            self.login(role)
            self.assertEqual(self.client.post(url, {"weekday": 3, "start_time": "09:00", "end_time": "10:00"}).status_code, 403)

    def test_database_rejects_inverted_window(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrainerAvailability.objects.create(trainer=self.trainer, weekday=1, start_time=time(10), end_time=time(9))

    def test_time_off_validation(self):
        self.login("manager")
        url = reverse("trainers:timeoff_add", args=[self.trainer.pk])
        self.assertEqual(self.client.post(url, {"start_date": "2026-12-05", "end_date": "2026-12-01", "reason": "x"}).status_code, 200)
        self.assertEqual(self.client.post(url, {"start_date": "2026-12-01", "end_date": "2026-12-05", "reason": "Leave"}).status_code, 302)
        self.assertEqual(self.trainer.time_off.count(), 1)


class OffboardingTests(Base):
    def test_terminating_a_trainer_ends_assignments_and_cancels_future_sessions(self):
        services.assign_trainer(self.trainer, self.member, self.staff)
        give_membership(self.member, self.staff)
        appt = appointment_services.book(self.member, self.trainer, future_slot(3), 60, self.staff)
        self.login("manager")
        data = {"full_name": "Coach Kim", "phone": self.trainer.phone, "email": "", "specialties": "", "qualifications": "", "bio": "",
                "employment_status": "terminated", "hire_date": self.trainer.hire_date.isoformat(), "hourly_rate": "3000", "user": self.users["trainer"].pk}
        r = self.client.post(reverse("trainers:edit", args=[self.trainer.pk]), data)
        self.assertEqual(r.status_code, 302)
        appt.refresh_from_db()
        a = TrainerAssignment.objects.get()
        self.assertEqual((appt.status, a.end_date is not None), ("cancelled", True))
        self.assertEqual(Appointment.objects.count(), 1)  # kept as history, not deleted

"""load_sample_data / cleanup_sample_data: repeatable, tracked, and safe for real data and the Super Admin."""
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.attendance import services as attendance
from apps.attendance.models import Attendance
from apps.core.demo import DEMO_PASSWORD
from apps.core.models import SampleRecord
from apps.members.models import Member
from apps.memberships import services as membership_services
from apps.memberships.models import MembershipPlan
from apps.payments import services as payment_services
from apps.payments.models import Payment


def run(command, *args):
    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


@override_settings(DEBUG=True)
class SampleDataTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser("realadmin", "admin@example.com", "Real-Admin-Passw0rd!")
        self.admin.role = Role.SUPER_ADMIN
        self.admin.save()
        self.admin_hash = User.objects.get(pk=self.admin.pk).password
        # genuine data that must survive everything below
        self.real = Member.objects.create(full_name="Real Person", phone="0712345678", date_of_birth="1990-01-01",
                                          emergency_contact_name="K", emergency_contact_phone="0719876543")
        plan = MembershipPlan.objects.create(name="Real Plan", price=4000)
        self.real_ms = membership_services.assign_membership(self.real, plan, timezone.localdate(), self.admin)
        self.real_pay, _ = payment_services.record_payment(self.real_ms, 1000, "cash", self.admin, "real1")
        self.real_visit = attendance.check_in(self.real, self.admin)

    def test_refuses_in_production_mode(self):
        with override_settings(DEBUG=False):
            for command in ("load_sample_data", "cleanup_sample_data"):
                with self.assertRaisesMessage(CommandError, "Refusing"):
                    run(command, "--yes") if command == "cleanup_sample_data" else run(command)
        self.assertFalse(SampleRecord.objects.exists())

    def test_load_is_repeatable_and_tracks_everything(self):
        out = run("load_sample_data")
        self.assertIn("Sample data loaded", out)
        self.assertIn(DEMO_PASSWORD, out)
        total = SampleRecord.objects.count()
        members = Member.objects.count()
        with self.assertRaisesMessage(CommandError, "already loaded"):
            run("load_sample_data")
        self.assertEqual((SampleRecord.objects.count(), Member.objects.count()), (total, members))  # no duplicates
        self.assertFalse(SampleRecord.objects.filter(model_label="members.Member", object_pk=str(self.real.pk)).exists())
        self.assertTrue(SampleRecord.objects.filter(model_label="payments.Payment").exists())
        self.assertTrue(SampleRecord.objects.filter(model_label="attendance.Attendance").exists())

    def test_sample_accounts_authenticate_and_have_no_admin_rights(self):
        run("load_sample_data")
        expected = {"demo_manager": "manager", "demo_reception": "receptionist", "demo_accountant": "accountant",
                    "demo_trainer": "trainer", "demo_alice": "member", "demo_bob": "member"}
        for username, role in expected.items():
            self.assertTrue(self.client.login(username=username, password=DEMO_PASSWORD), username)
            user = User.objects.get(username=username)
            self.assertEqual(user.role, role)
            self.assertFalse(user.is_superuser or user.is_staff, username)
            self.client.logout()
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)  # still only the real admin
        # role limits: a member cannot open staff pages, reception can scan
        self.client.login(username="demo_alice", password=DEMO_PASSWORD)
        self.assertEqual(self.client.get("/members/").status_code, 403)
        self.client.login(username="demo_reception", password=DEMO_PASSWORD)
        self.assertEqual(self.client.get("/attendance/scan/").status_code, 200)

    def test_members_memberships_payments_attendance_and_qr_are_linked(self):
        run("load_sample_data")
        sample_members = Member.objects.filter(full_name__startswith="Demo ")
        self.assertGreaterEqual(sample_members.count(), 9)
        tokens = [m.qr_token for m in sample_members]
        self.assertTrue(all(tokens) and len(set(tokens)) == len(tokens))
        alice = Member.objects.get(full_name="Demo Alice")
        self.assertEqual(alice.user.username, "demo_alice")
        self.assertTrue(alice.memberships.exists() and Payment.objects.filter(membership__member=alice).exists())
        self.assertTrue(Attendance.objects.filter(member=alice, method="qr").exists())
        for visit in Attendance.objects.filter(member__in=sample_members):
            self.assertTrue(visit.membership_id is None or visit.membership.member_id == visit.member_id)
        # a QR scan through the real service works for a sample member (George was checked in by a scan; scanning again within the cooldown is ignored)
        george = Member.objects.get(full_name="Demo George")
        with self.assertRaisesMessage(attendance.CheckInDenied, "repeated scan"):
            attendance.scan_qr(attendance.qr_payload(george), self.admin)
        with self.assertRaisesMessage(attendance.CheckInDenied, "deactivated"):
            attendance.scan_qr(attendance.qr_payload(Member.objects.get(full_name="Demo Ian")), self.admin)

    def test_cleanup_removes_only_sample_rows_and_leaves_real_data_and_admin(self):
        before = {"members": Member.objects.count(), "users": User.objects.count(), "payments": Payment.objects.count(), "visits": Attendance.objects.count()}
        run("load_sample_data")
        self.assertGreater(Member.objects.count(), before["members"])
        out = run("cleanup_sample_data", "--yes")
        self.assertIn("Removed", out)
        self.assertEqual(SampleRecord.objects.count(), 0)
        self.assertEqual({"members": Member.objects.count(), "users": User.objects.count(), "payments": Payment.objects.count(),
                          "visits": Attendance.objects.count()}, before)
        self.assertFalse(Member.objects.filter(full_name__startswith="Demo ").exists())
        self.assertFalse(User.objects.filter(username__startswith="demo_").exists())
        for obj in (self.real, self.real_ms, self.real_pay, self.real_visit):
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())
        admin = User.objects.get(pk=self.admin.pk)
        self.assertEqual((admin.username, admin.password, admin.is_superuser, admin.role), ("realadmin", self.admin_hash, True, Role.SUPER_ADMIN))
        self.assertTrue(self.client.login(username="realadmin", password="Real-Admin-Passw0rd!"))
        self.assertIn("nothing to remove", run("cleanup_sample_data", "--yes"))
        # can be loaded again after cleanup
        self.assertIn("Sample data loaded", run("load_sample_data"))

    def test_real_data_attached_to_sample_records_is_preserved(self):
        run("load_sample_data")
        alice = Member.objects.get(full_name="Demo Alice")
        ms = alice.memberships.first()
        real_payment, _ = payment_services.record_payment(ms, 10, "cash", self.admin, "real-on-sample")  # a real payment recorded afterwards
        out = run("cleanup_sample_data", "--yes")
        self.assertIn("Kept", out)
        self.assertTrue(Payment.objects.filter(pk=real_payment.pk).exists())
        self.assertTrue(Member.objects.filter(pk=alice.pk).exists())  # still referenced, so preserved
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_existing_untracked_demo_records_block_loading(self):
        Member.objects.create(full_name="Demo Existing", phone="0711111111", date_of_birth="1990-01-01", emergency_contact_name="K", emergency_contact_phone="0712222222")
        with self.assertRaisesMessage(CommandError, "not created by this loader"):
            run("load_sample_data")
        self.assertFalse(SampleRecord.objects.exists())
        self.assertFalse(User.objects.filter(username="demo_manager").exists())  # nothing half-created

    def test_works_without_an_existing_super_admin_without_creating_one(self):
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        run("load_sample_data")
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
        self.assertEqual(User.objects.filter(role=Role.SUPER_ADMIN).count(), 1)

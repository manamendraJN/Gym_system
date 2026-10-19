from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.testing import PASSWORD, make_member_with_login, make_trainer, make_users
from apps.trainers import services as trainer_services

from . import services
from .models import MeasurementType, ProgressMeasurement, ProgressRecord


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"])
        self.member, self.member_login = make_member_with_login(self.users, "Alice Member")
        self.other, self.other_login = make_member_with_login(self.users, "Other Member")
        trainer_services.assign_trainer(self.trainer, self.member, self.users["manager"])
        self.waist = MeasurementType.objects.get(name="Waist")

    def login(self, role=None, user=None):
        self.client.logout()
        user = user or self.users[role]
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def data(self, **kw):
        d = {"recorded_on": timezone.localdate().isoformat(), "weight_kg": "72.5", "height_cm": "175", "notes": "Feeling strong", f"m_{self.waist.pk}": "80.5"}
        d.update(kw)
        return d


class ModelAndConfigTests(Base):
    def test_default_measurement_types_are_seeded(self):
        self.assertTrue({"Body fat", "Waist", "Chest", "Hips", "Arm", "Thigh"} <= set(MeasurementType.objects.values_list("name", flat=True)))

    def test_bmi_is_indicative_only(self):
        r = ProgressRecord(member=self.member, weight_kg=Decimal("72.5"), height_cm=Decimal("175"))
        self.assertEqual(r.bmi, 23.7)
        self.assertIsNone(ProgressRecord(member=self.member, weight_kg=Decimal("72.5")).bmi)
        self.assertFalse(hasattr(r, "bmi_category"))  # no automated diagnosis labels

    def test_database_ranges(self):
        for kw in ({"weight_kg": Decimal("5")}, {"weight_kg": Decimal("500")}, {"height_cm": Decimal("50")}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                ProgressRecord.objects.create(member=self.member, **kw)
        r = ProgressRecord.objects.create(member=self.member, weight_kg=Decimal("70"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProgressMeasurement.objects.create(record=r, type=self.waist, value=Decimal("0"))

    def test_chart_data_aligns_series(self):
        today = timezone.localdate()
        r1 = ProgressRecord.objects.create(member=self.member, recorded_on=today - timedelta(days=7), weight_kg=Decimal("74"))
        r2 = ProgressRecord.objects.create(member=self.member, recorded_on=today, weight_kg=Decimal("72"))
        ProgressMeasurement.objects.create(record=r2, type=self.waist, value=Decimal("81"))
        c = services.chart_data(self.member)
        self.assertEqual(c["weight"], [74.0, 72.0])
        self.assertEqual(c["measurements"], [{"label": "Waist (cm)", "values": [None, 81.0]}])
        self.assertEqual(len(c["labels"]), 2)


class StaffProgressTests(Base):
    def test_trainer_records_for_assigned_member(self):
        self.login("trainer")
        url = reverse("progress:member", args=[self.member.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, self.data())
        self.assertRedirects(r, url)
        rec = ProgressRecord.objects.get()
        self.assertEqual((rec.member, rec.source, rec.recorded_by, rec.weight_kg), (self.member, "staff", self.users["trainer"], Decimal("72.5")))
        self.assertEqual(rec.measurements.get().value, Decimal("80.5"))

    def test_validation(self):
        self.login("trainer")
        url = reverse("progress:member", args=[self.member.pk])
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        for bad in (self.data(weight_kg="5"), self.data(weight_kg="900"), self.data(height_cm="30"), self.data(recorded_on=tomorrow),
                    self.data(**{f"m_{self.waist.pk}": "-3"}), self.data(weight_kg="", height_cm="", notes="x", **{f"m_{self.waist.pk}": ""})):
            self.assertEqual(self.client.post(url, bad).status_code, 200)
        self.assertFalse(ProgressRecord.objects.exists())

    def test_trainer_cannot_open_unassigned_members(self):
        self.login("trainer")
        url = reverse("progress:member", args=[self.other.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, self.data()).status_code, 404)
        self.assertFalse(ProgressRecord.objects.exists())
        listed = {m.pk for m in self.client.get(reverse("progress:list")).context["object_list"]}
        self.assertEqual(listed, {self.member.pk})

    def test_manager_sees_everyone_and_history(self):
        ProgressRecord.objects.create(member=self.other, weight_kg=Decimal("80"))
        self.login("manager")
        self.assertEqual(self.client.get(reverse("progress:member", args=[self.other.pk])).status_code, 200)
        self.assertEqual(len(self.client.get(reverse("progress:list")).context["object_list"]), 2)
        r = self.client.get(reverse("progress:member", args=[self.other.pk]))
        self.assertEqual(len(r.context["records"]), 1)
        self.assertContains(r, "not a diagnosis")

    def test_roles_without_permission(self):
        for role in ("receptionist", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.get(reverse("progress:member", args=[self.member.pk])).status_code, 403, role)
            self.assertEqual(self.client.get(reverse("progress:list")).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("progress:list")).status_code, 302)

    def test_measurement_type_management(self):
        self.login("trainer")
        self.assertEqual(self.client.post(reverse("progress:type_create"), {"name": "Calf", "unit": "cm", "position": 9, "is_active": "on"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("progress:type_create"), {"name": "waist", "unit": "cm", "position": 9}).status_code, 200)
        calf = MeasurementType.objects.get(name="Calf")
        self.client.post(reverse("progress:type_edit", args=[calf.pk]), {"name": "Calf", "unit": "cm", "position": 9})  # hidden
        r = self.client.get(reverse("progress:member", args=[self.member.pk]))
        self.assertNotIn(f"m_{calf.pk}", r.context["form"].fields)
        self.assertIn(f"m_{self.waist.pk}", r.context["form"].fields)
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("progress:types")).status_code, 403)

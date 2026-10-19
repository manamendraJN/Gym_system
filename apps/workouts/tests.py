from datetime import timedelta
import shutil
import tempfile
from io import BytesIO

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.core.models import Notification
from apps.core.testing import PASSWORD, make_member_with_login, make_trainer, make_users
from apps.members.tests import make_member
from apps.trainers import services as trainer_services

from . import services
from .models import Exercise, PlanChange, PlanExercise, SessionEntry, WorkoutPlan, WorkoutSession

TMP_MEDIA = tempfile.mkdtemp()


def png(name="x.png"):
    buf = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"])
        self.member, self.member_login = make_member_with_login(self.users, "Alice Member")
        self.other, _ = make_member_with_login(self.users, "Other Member")
        trainer_services.assign_trainer(self.trainer, self.member, self.users["manager"])
        self.squat = Exercise.objects.create(name="Squat", muscle_group="legs")
        self.row = Exercise.objects.create(name="Row", muscle_group="back")

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))

    def plan(self, member=None, **kw):
        member = member or self.member
        return WorkoutPlan.objects.create(member=member, trainer=self.trainer, name=kw.pop("name", "Strength"), **kw)

    def item(self, plan, exercise=None, **kw):
        data = dict(weekday=0, sets=3, reps=10)
        data.update(kw)
        return PlanExercise.objects.create(plan=plan, exercise=exercise or self.squat, **data)


class ExerciseTests(Base):
    def form(self, **kw):
        data = {"name": "Deadlift", "muscle_group": "back", "category": "strength", "description": "Hinge", "instructions": "Keep your back flat",
                "video_url": "https://example.com/v", "is_active": "on"}
        data.update(kw)
        return data

    def test_trainer_manages_library(self):
        self.login("trainer")
        r = self.client.post(reverse("workouts:exercise_create"), {**self.form(), "image": png()})
        e = Exercise.objects.get(name="Deadlift")
        self.assertRedirects(r, e.get_absolute_url(), fetch_redirect_response=False)
        self.assertTrue(e.image.name.startswith("exercises/"))
        self.assertEqual(self.client.get(e.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.post(reverse("workouts:exercise_edit", args=[e.pk]), self.form(description="Updated")).status_code, 302)

    def test_validation(self):
        self.login("manager")
        for bad in (self.form(name="squat"), self.form(video_url="http://insecure.example"), self.form(video_url="javascript:alert(1)")):
            self.assertEqual(self.client.post(reverse("workouts:exercise_create"), bad).status_code, 200)
        fake = SimpleUploadedFile("a.png", b"nope", content_type="image/png")
        self.assertEqual(self.client.post(reverse("workouts:exercise_create"), {**self.form(), "image": fake}).status_code, 200)
        self.assertEqual(Exercise.objects.count(), 2)

    def test_permissions_and_filters(self):
        for role, view, manage in (("trainer", 200, 200), ("manager", 200, 200), ("receptionist", 403, 403), ("accountant", 403, 403), ("member", 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("workouts:exercise_list")).status_code, view, role)
            self.assertEqual(self.client.get(reverse("workouts:exercise_create")).status_code, manage, role)
        self.login("trainer")
        names = lambda **p: {e.name for e in self.client.get(reverse("workouts:exercise_list"), p).context["object_list"]}
        self.assertEqual(names(muscle="legs"), {"Squat"})
        self.assertEqual(names(q="ro"), {"Row"})
        self.assertEqual(names(category="cardio"), set())
        self.row.is_active = False
        self.row.save()
        self.assertEqual(names(), {"Squat"})  # inactive exercises are hidden from pickers

    def test_exercise_constraints(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Exercise.objects.create(name="SQUAT", muscle_group="legs")


class PlanTests(Base):
    def form(self, **kw):
        data = {"name": "Push Pull", "member": self.member.member_id, "goal": "Strength", "description": "", "start_date": timezone.localdate().isoformat(), "end_date": ""}
        data.update(kw)
        return data

    def test_trainer_creates_plan_for_assigned_member(self):
        self.login("trainer")
        r = self.client.post(reverse("workouts:plan_create"), self.form())
        plan = WorkoutPlan.objects.get(name="Push Pull")
        self.assertRedirects(r, plan.get_absolute_url())
        self.assertEqual((plan.member, plan.trainer, plan.created_by), (self.member, self.trainer, self.users["trainer"]))
        self.assertEqual(plan.changes.get().description, "Plan created")
        self.assertTrue(Notification.objects.filter(user=self.member_login, title="New workout plan").exists())

    def test_trainer_cannot_create_for_unassigned_member_but_manager_can(self):
        self.login("trainer")
        r = self.client.post(reverse("workouts:plan_create"), self.form(member=self.other.member_id))
        self.assertContains(r, "assigned to you")
        self.assertFalse(WorkoutPlan.objects.exists())
        self.login("manager")
        self.assertEqual(self.client.post(reverse("workouts:plan_create"), self.form(member=self.other.member_id, trainer=self.trainer.pk)).status_code, 302)

    def test_dates_validated(self):
        self.login("trainer")
        r = self.client.post(reverse("workouts:plan_create"), self.form(start_date="2026-05-10", end_date="2026-05-01"))
        self.assertEqual(r.status_code, 200)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkoutPlan.objects.create(member=self.member, name="Bad", start_date="2026-05-10", end_date="2026-05-01")

    def test_add_edit_and_group_exercises_by_day(self):
        plan = self.plan()
        self.login("trainer")
        url = reverse("workouts:item_add", args=[plan.pk])
        base = {"exercise": self.squat.pk, "weekday": 0, "sets": 4, "reps": 8, "weight_kg": "60.5", "rest_seconds": 90, "notes": ""}
        self.assertEqual(self.client.post(url, base).status_code, 302)
        self.assertEqual(self.client.post(url, {**base, "exercise": self.row.pk, "weekday": 2, "reps": "", "duration_minutes": 15}).status_code, 302)
        self.assertEqual(self.client.post(url, {**base, "reps": "", "weight_kg": ""}).status_code, 200)  # neither reps nor duration
        self.assertEqual(self.client.post(url, {**base, "sets": 0}).status_code, 200)
        self.assertEqual(self.client.post(url, {**base, "sets": 99}).status_code, 200)
        self.assertEqual(plan.items.count(), 2)
        r = self.client.get(plan.get_absolute_url())
        self.assertEqual([d["label"] for d in r.context["days"]], ["Monday", "Wednesday"])
        first = plan.items.get(weekday=0)
        self.assertEqual(first.prescription, "4 × 8 · 60.5 kg · rest 90s")
        self.assertEqual(self.client.post(reverse("workouts:item_edit", args=[first.pk]), {**base, "sets": 5}).status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.sets, 5)
        self.assertTrue(plan.changes.filter(description__startswith="Updated").exists())

    def test_database_constraints_on_items(self):
        plan = self.plan()
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlanExercise.objects.create(plan=plan, exercise=self.squat, weekday=0, sets=3)  # no reps or duration
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlanExercise.objects.create(plan=plan, exercise=self.squat, weekday=9, sets=3, reps=5)

    def test_removing_an_exercise_with_history_archives_it(self):
        plan = self.plan()
        used, unused = self.item(plan), self.item(plan, self.row)
        session = services.log_session(self.member, plan, timezone.localdate(), [{"item": used, "sets": 3, "reps": 10, "weight": None, "duration": None}], self.users["trainer"])
        self.assertEqual(services.remove_item(used, self.users["trainer"]), "archived")
        self.assertEqual(services.remove_item(unused, self.users["trainer"]), "deleted")
        used.refresh_from_db()
        self.assertFalse(used.is_active)
        self.assertEqual(session.entries.get().plan_item_id, used.pk)  # history intact
        self.assertEqual(PlanChange.objects.filter(plan=plan).count(), 2)
        self.login("trainer")
        r = self.client.get(plan.get_absolute_url())
        self.assertEqual(r.context["days"], [])
        self.assertEqual([i.pk for i in r.context["archived"]], [used.pk])

    def test_logged_numbers_are_a_snapshot_not_affected_by_later_plan_edits(self):
        plan = self.plan()
        item = self.item(plan, sets=3, reps=10, weight_kg="50")
        services.log_session(self.member, plan, timezone.localdate(), [{"item": item, "sets": 3, "reps": 10, "weight": item.weight_kg, "duration": None}], self.users["trainer"])
        item.sets, item.reps, item.weight_kg = 5, 5, "100"
        item.save()
        entry = SessionEntry.objects.get()
        self.assertEqual((entry.sets_done, entry.reps_done, str(entry.weight_kg)), (3, 10, "50.00"))

    def test_copy_plan_to_another_member(self):
        plan = self.plan()
        self.item(plan)
        gone = self.item(plan, self.row, weekday=1)
        gone.is_active = False
        gone.save()
        self.login("manager")
        r = self.client.post(reverse("workouts:plan_copy", args=[plan.pk]), {"member": self.other.member_id, "start_date": timezone.localdate().isoformat()})
        copy = WorkoutPlan.objects.exclude(pk=plan.pk).get()
        self.assertRedirects(r, copy.get_absolute_url())
        self.assertEqual((copy.member, copy.copied_from, copy.items.count()), (self.other, plan, 1))  # archived items are not copied
        self.assertEqual(plan.items.count(), 2)
        # a trainer cannot copy onto a member who is not theirs
        self.login("trainer")
        r = self.client.post(reverse("workouts:plan_copy", args=[plan.pk]), {"member": self.other.member_id, "start_date": timezone.localdate().isoformat()})
        self.assertContains(r, "assigned to you")

    def test_status_changes_and_edit(self):
        plan = self.plan()
        self.login("trainer")
        self.client.post(reverse("workouts:plan_status", args=[plan.pk]), {"status": "completed"})
        plan.refresh_from_db()
        self.assertEqual(plan.status, "completed")
        self.client.post(reverse("workouts:plan_status", args=[plan.pk]), {"status": "bogus"})
        plan.refresh_from_db()
        self.assertEqual(plan.status, "completed")
        self.assertEqual(self.client.get(reverse("workouts:plan_status", args=[plan.pk])).status_code, 405)
        r = self.client.post(reverse("workouts:plan_edit", args=[plan.pk]), {"name": "Renamed", "goal": "", "description": "", "start_date": plan.start_date.isoformat(), "end_date": ""})
        self.assertEqual(r.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual((plan.name, plan.member), ("Renamed", self.member))

    def test_scoping_and_permissions(self):
        mine, theirs = self.plan(), self.plan(self.other, name="Not mine")
        self.login("trainer")
        self.assertEqual({p.pk for p in self.client.get(reverse("workouts:plan_list")).context["object_list"]}, {mine.pk})
        self.assertEqual(self.client.get(theirs.get_absolute_url()).status_code, 404)
        item = self.item(theirs)
        self.assertEqual(self.client.get(reverse("workouts:item_edit", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("workouts:item_remove", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("workouts:plan_log", args=[theirs.pk])).status_code, 404)
        self.assertTrue(PlanExercise.objects.filter(pk=item.pk).exists())
        self.login("manager")
        self.assertEqual({p.pk for p in self.client.get(reverse("workouts:plan_list")).context["object_list"]}, {mine.pk, theirs.pk})
        for role in ("receptionist", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.get(reverse("workouts:plan_list")).status_code, 403, role)
            self.assertEqual(self.client.get(mine.get_absolute_url()).status_code, 403, role)


class LogWorkoutTests(Base):
    def setUp(self):
        super().setUp()
        self.plan_ = self.plan()
        self.a = self.item(self.plan_, self.squat, weekday=0, sets=3, reps=10, weight_kg="60")
        self.b = self.item(self.plan_, self.row, weekday=1, sets=4, reps=8)

    def post(self, **kw):
        data = {"performed_on": timezone.localdate().isoformat(), "notes": "Felt good", f"done_{self.a.pk}": "on",
                f"sets_{self.a.pk}": 3, f"reps_{self.a.pk}": 8, f"weight_{self.a.pk}": "62.5", f"duration_{self.a.pk}": ""}
        data.update(kw)
        return self.client.post(reverse("workouts:plan_log", args=[self.plan_.pk]), data)

    def test_trainer_records_workout_on_behalf(self):
        self.login("trainer")
        self.assertEqual(self.post().status_code, 302)
        s = WorkoutSession.objects.get()
        e = s.entries.get()
        self.assertEqual((s.member, s.recorded_by, e.plan_item, e.reps_done, str(e.weight_kg)), (self.member, self.users["trainer"], self.a, 8, "62.50"))

    def test_day_filter_limits_the_rows(self):
        self.login("trainer")
        r = self.client.get(reverse("workouts:plan_log", args=[self.plan_.pk]), {"day": 1})
        self.assertEqual([row["item"].pk for row in r.context["form"].rows], [self.b.pk])

    def test_validation(self):
        self.login("trainer")
        self.assertEqual(self.post(**{f"done_{self.a.pk}": ""}).status_code, 200)  # nothing ticked
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.post(performed_on=tomorrow).status_code, 200)
        before = (self.plan_.start_date - timedelta(days=1)).isoformat()
        self.assertEqual(self.post(performed_on=before).status_code, 200)
        self.assertEqual(self.post(**{f"sets_{self.a.pk}": 500}).status_code, 200)
        self.assertEqual(self.post(**{f"weight_{self.a.pk}": "-5"}).status_code, 200)
        self.assertFalse(WorkoutSession.objects.exists())

    def test_cannot_log_for_other_members_plan(self):
        theirs = self.plan(self.other)
        self.login("trainer")
        self.assertEqual(self.client.post(reverse("workouts:plan_log", args=[theirs.pk]), {}).status_code, 404)

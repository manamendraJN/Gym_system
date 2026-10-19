"""Guided workout runs: set-by-set tracking, rest timing, ownership and recovery. Time is passed in explicitly, so the
tests never sleep."""
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.testing import PASSWORD, make_member_with_login, make_trainer, make_users
from apps.trainers import services as trainer_services

from . import runs
from .models import Exercise, ExerciseRun, ExerciseStatus, PlanExercise, RunStatus, SetRun, WorkoutPlan, WorkoutRun, WorkoutSession

T0 = timezone.now().replace(microsecond=0)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"])
        self.member, self.login_a = make_member_with_login(self.users, "Alice Member")
        self.other, self.login_b = make_member_with_login(self.users, "Bob Other")
        trainer_services.assign_trainer(self.trainer, self.member, self.users["manager"])
        self.squat = Exercise.objects.create(name="Squat", muscle_group="legs", instructions="Chest up.")
        self.row = Exercise.objects.create(name="Row", muscle_group="back")
        self.plan = WorkoutPlan.objects.create(member=self.member, trainer=self.trainer, name="Strength")
        # trainer order: Row first (position 1), Squat second (position 2) even though Squat was created first
        self.squat_item = PlanExercise.objects.create(plan=self.plan, exercise=self.squat, weekday=0, position=2, sets=3, reps=8, rest_seconds=60)
        self.row_item = PlanExercise.objects.create(plan=self.plan, exercise=self.row, weekday=0, position=1, sets=2, reps=10, rest_seconds=30)
        PlanExercise.objects.create(plan=self.plan, exercise=self.row, weekday=3, position=1, sets=1, reps=5)  # another day

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def start(self):
        run, created = runs.start_run(self.member, self.plan, 0, now=at(0))
        return run

    def do_exercise(self, run, ex, t, work=40):
        """Run every set of an exercise, waiting out each rest. Returns the time after the last set."""
        runs.start_exercise(run, ex.pk, now=at(t))
        for n in range(1, ex.target_sets + 1):
            s = SetRun.objects.get(exercise_run=ex, number=n)
            t += work
            runs.finish_set(run, s.pk, now=at(t))
            if n < ex.target_sets:
                t += ex.rest_seconds
                runs.start_next_set(run, ex.pk, now=at(t))
        return t


class RunRulesTests(Base):
    def test_start_copies_plan_in_trainer_order_and_is_not_duplicated(self):
        run = self.start()
        names = [e.exercise.name for e in run.exercises.all()]
        self.assertEqual(names, ["Row", "Squat"])  # position, not creation order
        self.assertEqual([(e.target_sets, e.target_reps, e.rest_seconds, e.status) for e in run.exercises.all()],
                         [(2, 10, 30, "pending"), (3, 8, 60, "pending")])
        again, created = runs.start_run(self.member, self.plan, 0, now=at(5))
        self.assertEqual((again.pk, created), (run.pk, False))
        self.assertEqual(WorkoutRun.objects.count(), 1)
        self.squat_item.sets = 9
        self.squat_item.save()
        self.assertEqual(run.exercises.get(exercise=self.squat).target_sets, 3)  # later plan edits do not change a run in progress

    def test_start_validation(self):
        with self.assertRaisesMessage(runs.RunError, "no exercises planned"):
            runs.start_run(self.member, self.plan, 5, now=at(0))
        self.plan.status = "archived"
        self.plan.save()
        with self.assertRaisesMessage(runs.RunError, "no longer active"):
            runs.start_run(self.member, self.plan, 0, now=at(0))
        other_plan = WorkoutPlan.objects.create(member=self.other, trainer=self.trainer, name="Theirs")
        PlanExercise.objects.create(plan=other_plan, exercise=self.row, weekday=0, sets=1, reps=5)
        with self.assertRaisesMessage(runs.RunError, "not assigned to you"):
            runs.start_run(self.member, other_plan, 0, now=at(0))

    def test_database_allows_only_one_active_run_per_member(self):
        self.start()
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkoutRun.objects.create(member=self.member, plan=self.plan, weekday=0)

    def test_exercises_start_in_order_one_at_a_time(self):
        run = self.start()
        row, squat = list(run.exercises.all())
        with self.assertRaisesMessage(runs.RunError, "in order"):
            runs.start_exercise(run, squat.pk, now=at(1))
        runs.start_exercise(run, row.pk, now=at(1))
        runs.start_exercise(run, row.pk, now=at(2))  # a double click does not restart it
        self.assertEqual(ExerciseRun.objects.get(pk=row.pk).started_at, at(1))
        with self.assertRaisesMessage(runs.RunError, "Finish the current exercise"):
            runs.start_exercise(run, squat.pk, now=at(3))

    def test_set_by_set_with_rest_and_server_timestamps(self):
        run = self.start()
        row = run.exercises.get(position=1)
        runs.start_exercise(run, row.pk, now=at(10))
        s1 = SetRun.objects.get(exercise_run=row, number=1)
        self.assertEqual(s1.started_at, at(10))  # starting the exercise starts set 1
        _, ex_done, run_done = runs.finish_set(run, s1.pk, now=at(55))
        self.assertEqual((ex_done, run_done), (False, False))
        self.assertEqual(SetRun.objects.get(pk=s1.pk).elapsed, timedelta(seconds=45))
        state = runs.build_state(run, now=at(65))
        self.assertEqual((state["phase"], int(state["current"].rest_remaining), state["current"].set_number), ("resting", 20, 2))
        with self.assertRaisesMessage(runs.RunError, "Rest is not finished"):
            runs.start_next_set(run, row.pk, now=at(65))  # 20 s of the 30 s rest are left
        self.assertEqual(row.sets.count(), 1)
        self.assertEqual(runs.build_state(run, now=at(86))["phase"], "ready")  # waits for the member: nothing starts by itself
        self.assertEqual(row.sets.count(), 1)
        s2 = runs.start_next_set(run, row.pk, now=at(90))
        self.assertEqual((s2.number, s2.rest_before_seconds, s2.rest_before_actual), (2, 30, timedelta(seconds=35)))
        again = runs.start_next_set(run, row.pk, now=at(91))  # double click while set 2 is running
        self.assertEqual(again.pk, s2.pk)
        self.assertEqual(row.sets.count(), 2)

    def test_finishing_a_set_twice_never_duplicates(self):
        run = self.start()
        row = run.exercises.get(position=1)
        runs.start_exercise(run, row.pk, now=at(0))
        s1 = SetRun.objects.get(exercise_run=row, number=1)
        runs.finish_set(run, s1.pk, now=at(30))
        with self.assertRaisesMessage(runs.RunError, "already finished"):
            runs.finish_set(run, s1.pk, now=at(31))
        self.assertEqual(SetRun.objects.get(pk=s1.pk).completed_at, at(30))
        self.assertEqual(row.sets.filter(completed_at__isnull=False).count(), 1)

    def test_full_workout_completes_and_writes_history(self):
        run = self.start()
        row, squat = list(run.exercises.all())
        t = self.do_exercise(run, row, 10)
        row.refresh_from_db()
        self.assertEqual(row.status, ExerciseStatus.COMPLETED)
        self.assertEqual(runs.build_state(run, now=at(t + 1))["next_exercise"].pk, squat.pk)  # waits for an explicit start
        self.assertEqual(WorkoutRun.objects.get(pk=run.pk).status, RunStatus.ACTIVE)
        t = self.do_exercise(run, squat, t + 20)
        run.refresh_from_db()
        self.assertEqual((run.status, run.completed_at), (RunStatus.COMPLETED, at(t)))
        self.assertEqual(run.duration, timedelta(seconds=t))
        state = runs.build_state(run)
        self.assertEqual((state["done_sets"], state["total_sets"], state["percent"], state["done_exercises"]), (5, 5, 100, 2))
        session = run.session
        self.assertEqual({(e.exercise.name, e.sets_done) for e in session.entries.all()}, {("Row", 2), ("Squat", 3)})
        self.assertEqual(WorkoutSession.objects.filter(member=self.member).count(), 1)
        with self.assertRaisesMessage(runs.RunError, "already ended"):
            runs.finish_set(run, SetRun.objects.first().pk, now=at(t + 5))

    def test_ending_early_keeps_what_was_done(self):
        run = self.start()
        row, squat = list(run.exercises.all())
        runs.start_exercise(run, row.pk, now=at(0))
        s1 = SetRun.objects.get(exercise_run=row, number=1)
        runs.finish_set(run, s1.pk, now=at(30))
        runs.end_run(run, now=at(100))
        run.refresh_from_db()
        row.refresh_from_db()
        squat.refresh_from_db()
        self.assertEqual((run.status, row.status, squat.status), (RunStatus.ABANDONED, ExerciseStatus.SKIPPED, ExerciseStatus.SKIPPED))
        self.assertEqual([(e.exercise.name, e.sets_done) for e in run.session.entries.all()], [("Row", 1)])
        self.assertEqual(runs.build_state(run)["done_sets"], 1)
        # a new workout can be started afterwards
        self.assertTrue(runs.start_run(self.member, self.plan, 0, now=at(200))[1])

    def test_stale_run_is_closed_automatically(self):
        run = self.start()
        runs.close_stale_runs(self.member, now=at(3600))
        self.assertEqual(WorkoutRun.objects.get(pk=run.pk).status, RunStatus.ACTIVE)
        runs.close_stale_runs(self.member, now=at(13 * 3600))
        run.refresh_from_db()
        self.assertEqual((run.status, run.completed_at), (RunStatus.ABANDONED, at(12 * 3600)))


class RunViewTests(Base):
    def test_full_flow_through_the_portal_and_refresh_restores_the_timer(self):
        self.login(self.login_a)
        r = self.client.post(reverse("portal:run_start", args=[self.plan.pk]), {"weekday": 0}, follow=True)
        run = WorkoutRun.objects.get()
        self.assertContains(r, "Start Exercise")
        self.assertContains(r, "Row")
        row = run.exercises.get(position=1)
        squat = run.exercises.get(position=2)
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "disabled")  # Squat's Start Exercise is locked until Row is done
        self.client.post(reverse("portal:run_exercise_start", args=[run.pk, row.pk]))
        s1 = SetRun.objects.get(exercise_run=row, number=1)
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "Finish Set")
        self.assertContains(page, "Set 1 of 2")
        self.client.post(reverse("portal:run_finish_set", args=[run.pk, s1.pk]))
        s1.refresh_from_db()
        page = self.client.get(run.get_absolute_url())
        self.assertEqual(page.context["state"]["phase"], "resting")
        self.assertContains(page, f'data-deadline="{int((s1.completed_at + timedelta(seconds=30)).timestamp())}000"')
        # a refresh does not move the deadline or create records
        again = self.client.get(run.get_absolute_url())
        self.assertContains(again, f'data-deadline="{int((s1.completed_at + timedelta(seconds=30)).timestamp())}000"')
        self.assertEqual(SetRun.objects.filter(exercise_run=row).count(), 1)
        # double submit of "Finish Set" and an early "Start Set" are refused with a message
        r = self.client.post(reverse("portal:run_finish_set", args=[run.pk, s1.pk]), follow=True)
        self.assertContains(r, "already finished")
        r = self.client.post(reverse("portal:run_next_set", args=[run.pk, row.pk]), follow=True)
        self.assertContains(r, "Rest is not finished")
        self.assertEqual(SetRun.objects.filter(exercise_run=row).count(), 1)
        # once the rest has passed, the page offers the next set and never starts it itself
        SetRun.objects.filter(pk=s1.pk).update(started_at=T0 - timedelta(minutes=5), completed_at=T0 - timedelta(minutes=4))
        page = self.client.get(run.get_absolute_url())
        self.assertEqual(page.context["state"]["phase"], "ready")
        self.assertContains(page, "Start Set 2")
        self.assertEqual(SetRun.objects.filter(exercise_run=row).count(), 1)
        self.client.post(reverse("portal:run_next_set", args=[run.pk, row.pk]))
        self.assertEqual(SetRun.objects.filter(exercise_run=row).count(), 2)
        self.client.post(reverse("portal:run_end", args=[run.pk]), follow=True)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.ABANDONED)
        self.assertContains(self.client.get(run.get_absolute_url()), "Workout ended early")

    def test_members_can_only_use_their_own_runs_and_only_members_can_start(self):
        self.login(self.login_a)
        self.client.post(reverse("portal:run_start", args=[self.plan.pk]), {"weekday": 0})
        run = WorkoutRun.objects.get()
        row = run.exercises.get(position=1)
        self.login(self.login_b)
        for url, method in ((run.get_absolute_url(), "get"), (reverse("portal:run_end", args=[run.pk]), "post"),
                            (reverse("portal:run_exercise_start", args=[run.pk, row.pk]), "post")):
            self.assertEqual(getattr(self.client, method)(url).status_code, 404, url)
        self.assertEqual(self.client.post(reverse("portal:run_start", args=[self.plan.pk]), {"weekday": 0}).status_code, 404)
        self.assertEqual(WorkoutRun.objects.get().status, RunStatus.ACTIVE)
        for role in ("trainer", "receptionist", "manager"):
            self.login(self.users[role])
            self.assertEqual(self.client.get(run.get_absolute_url()).status_code, 403, role)
            self.assertEqual(self.client.post(reverse("portal:run_start", args=[self.plan.pk]), {"weekday": 0}).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(run.get_absolute_url()).status_code, 302)

    def test_get_requests_cannot_change_a_run(self):
        self.login(self.login_a)
        self.client.post(reverse("portal:run_start", args=[self.plan.pk]), {"weekday": 0})
        run = WorkoutRun.objects.get()
        row = run.exercises.get(position=1)
        for url in (reverse("portal:run_end", args=[run.pk]), reverse("portal:run_exercise_start", args=[run.pk, row.pk]),
                    reverse("portal:run_start", args=[self.plan.pk])):
            self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(run.exercises.filter(status="active").count(), 0)

    def test_page_offers_resume_and_trainer_sees_the_runs(self):
        self.login(self.login_a)
        self.client.post(reverse("portal:run_start", args=[self.plan.pk]), {"weekday": 0})
        run = WorkoutRun.objects.get()
        self.assertContains(self.client.get(reverse("portal:workout", args=[self.plan.pk])), "Resume workout")
        runs.end_run(run)
        self.assertContains(self.client.get(reverse("portal:workout", args=[self.plan.pk])), "Start Workout")
        self.login(self.users["trainer"])
        page = self.client.get(self.plan.get_absolute_url())
        self.assertContains(page, "Guided workouts")
        self.assertContains(page, "Ended early")


class TrainerConfigurationTests(Base):
    def test_trainer_sets_order_reps_and_rest_per_exercise(self):
        self.login(self.users["trainer"])
        url = reverse("workouts:item_add", args=[self.plan.pk])
        form = {"weekday": 0, "exercise": self.squat.pk, "position": "", "sets": 4, "reps": 6, "rest_seconds": 120, "notes": ""}
        self.assertEqual(self.client.post(url, form).status_code, 302)
        added = PlanExercise.objects.filter(plan=self.plan, weekday=0).order_by("-id").first()
        self.assertEqual((added.sets, added.reps, added.rest_seconds, added.position), (4, 6, 120, 3))  # appended after positions 1 and 2
        self.client.post(reverse("workouts:item_edit", args=[added.pk]), {**form, "position": 1, "rest_seconds": 45})
        added.refresh_from_db()
        self.assertEqual((added.position, added.rest_seconds), (1, 45))
        ordered = list(self.plan.items.filter(weekday=0).order_by("position", "id"))
        self.assertEqual([i.pk for i in ordered], [self.row_item.pk, added.pk, self.squat_item.pk])  # equal positions fall back to creation order
        self.client.post(reverse("workouts:item_edit", args=[added.pk]), {**form, "position": "", "rest_seconds": 45})
        added.refresh_from_db()
        self.assertEqual(added.position, 1)  # an empty order keeps the current one
        self.login(self.login_a)
        self.assertEqual(self.client.post(url, form).status_code, 403)  # members cannot configure plans


class PostgresLockingTests(TestCase):
    """SQLite ignores row locks, so these tests never caught a PostgreSQL-only error ("FOR UPDATE cannot be applied to the nullable
    side of an outer join"). Compile the SQL for PostgreSQL and check the lock is limited to the main table."""

    def compile_for_postgres(self, queryset):
        from unittest import mock

        from django.db.backends.postgresql.base import DatabaseWrapper

        settings_dict = {"NAME": "x", "USER": "", "PASSWORD": "", "HOST": "", "PORT": "", "OPTIONS": {}, "TIME_ZONE": None, "CONN_MAX_AGE": 0,
                         "AUTOCOMMIT": True, "ATOMIC_REQUESTS": False, "CONN_HEALTH_CHECKS": False, "TEST": {}}
        wrapper = DatabaseWrapper(settings_dict, alias="pg_check")
        wrapper.in_atomic_block = True
        with mock.patch.object(DatabaseWrapper, "get_autocommit", return_value=False), mock.patch.object(DatabaseWrapper, "ensure_connection"):
            return queryset.query.get_compiler(connection=wrapper).as_sql()[0]

    def test_locked_queries_with_joins_lock_only_their_own_table(self):
        from apps.memberships.models import Membership
        from apps.payments.models import Payment

        for queryset, table in ((WorkoutRun.objects.select_related("plan", "member").select_for_update(of=("self",)), "workouts_workoutrun"),
                                (SetRun.objects.select_related("exercise_run").select_for_update(of=("self",)), "workouts_setrun")):
            self.assertIn(f'FOR UPDATE OF "{table}"', self.compile_for_postgres(queryset))
        # the service functions build their own querysets; make sure none of them uses a bare lock together with a join
        import inspect

        from apps.memberships import services as membership_services
        from apps.payments import services as payment_services
        from apps.trainers import services as trainer_services

        for module in (runs, membership_services, payment_services, trainer_services):
            source = inspect.getsource(module)
            self.assertNotIn("select_for_update().select_related", source, module.__name__)

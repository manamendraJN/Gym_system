from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.testing import PASSWORD, make_member_with_login, make_trainer, make_users
from apps.trainers import services as trainer_services

from .models import DISCLAIMER, MealItem, MealPlan, NutritionProfile


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.trainer = make_trainer("Coach Kim", user=self.users["trainer"])
        self.member, self.member_login = make_member_with_login(self.users, "Alice Member")
        self.other, self.other_login = make_member_with_login(self.users, "Other Member")
        trainer_services.assign_trainer(self.trainer, self.member, self.users["manager"])

    def login(self, role=None, user=None):
        self.client.logout()
        user = user or self.users[role]
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def plan(self, member=None, **kw):
        return MealPlan.objects.create(member=member or self.member, name=kw.pop("name", "Lean plan"), **kw)

    def form(self, **kw):
        d = {"name": "Lean plan", "member": self.member.member_id, "goal": "Eat more vegetables", "notes": "", "start_date": timezone.localdate().isoformat(), "end_date": "", "is_active": "on"}
        d.update(kw)
        return d


class ModuleSwitchTests(Base):
    """The module is optional: with NUTRITION_ENABLED off nothing is reachable and nothing is listed."""

    def test_disabled_by_default_everything_404s(self):
        plan = self.plan()
        urls = [reverse("nutrition:plan_list"), reverse("nutrition:plan_create"), plan.get_absolute_url(), reverse("nutrition:plan_edit", args=[plan.pk]),
                reverse("nutrition:profile", args=[self.member.pk]), reverse("nutrition:item_add", args=[plan.pk])]
        for role in ("manager", "trainer"):
            self.login(role)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 404, (role, url))
            self.assertNotContains(self.client.get(reverse("core:home")), "Meal plans")
        self.login(user=self.member_login)
        self.assertEqual(self.client.get(reverse("portal:nutrition")).status_code, 404)
        self.assertNotContains(self.client.get(reverse("portal:home")), "My nutrition")

    @override_settings(NUTRITION_ENABLED=True)
    def test_enabled_shows_navigation(self):
        self.login("trainer")
        self.assertContains(self.client.get(reverse("core:home")), "Meal plans")
        self.login(user=self.member_login)
        self.assertContains(self.client.get(reverse("portal:home")), "My nutrition")


@override_settings(NUTRITION_ENABLED=True)
class MealPlanTests(Base):
    def test_trainer_creates_plan_for_assigned_member_only(self):
        self.login("trainer")
        r = self.client.post(reverse("nutrition:plan_create"), self.form())
        plan = MealPlan.objects.get()
        self.assertRedirects(r, plan.get_absolute_url())
        r = self.client.post(reverse("nutrition:plan_create"), self.form(member=self.other.member_id))
        self.assertContains(r, "assigned to you")
        self.assertEqual(MealPlan.objects.count(), 1)
        self.assertEqual(self.client.post(reverse("nutrition:plan_create"), self.form(start_date="2026-05-10", end_date="2026-05-01")).status_code, 200)

    def test_meals_by_day_and_meal_type(self):
        plan = self.plan()
        self.login("trainer")
        url = reverse("nutrition:item_add", args=[plan.pk])
        base = {"weekday": 0, "meal_type": "lunch", "title": "Chicken & rice", "description": "Grilled", "ingredients": "Chicken\nRice\n\nBroccoli"}
        self.assertEqual(self.client.post(url, base).status_code, 302)
        self.client.post(url, {**base, "meal_type": "breakfast", "title": "Oats", "ingredients": "Oats"})
        self.client.post(url, {**base, "weekday": 3, "title": "Fish"})
        self.assertEqual(self.client.post(url, {**base, "meal_type": "brunch"}).status_code, 200)
        r = self.client.get(plan.get_absolute_url())
        days = r.context["days"]
        self.assertEqual([d["label"] for d in days], ["Monday", "Thursday"])
        self.assertEqual([m.title for m in days[0]["meals"]], ["Oats", "Chicken & rice"])  # breakfast before lunch
        self.assertEqual(days[0]["meals"][1].ingredient_list, ["Chicken", "Rice", "Broccoli"])
        item = MealItem.objects.get(title="Fish")
        self.client.post(reverse("nutrition:item_delete", args=[item.pk]))
        self.assertFalse(MealItem.objects.filter(pk=item.pk).exists())

    def test_disclaimer_and_allergy_warning_are_shown_without_calculations(self):
        plan = self.plan()
        NutritionProfile.objects.create(member=self.member, allergies="Peanuts", food_preferences="Vegetarian")
        self.login("trainer")
        r = self.client.get(plan.get_absolute_url())
        self.assertContains(r, "not medical advice")
        self.assertContains(r, "Peanuts")
        self.assertNotContains(r, "calorie")
        self.assertIn("not medical advice", DISCLAIMER)

    def test_scoping_and_permissions(self):
        mine, theirs = self.plan(), self.plan(self.other, name="Not mine")
        self.login("trainer")
        self.assertEqual({p.pk for p in self.client.get(reverse("nutrition:plan_list")).context["object_list"]}, {mine.pk})
        self.assertEqual(self.client.get(theirs.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse("nutrition:profile", args=[self.other.pk])).status_code, 404)
        item = MealItem.objects.create(plan=theirs, weekday=0, meal_type="lunch", title="x")
        self.assertEqual(self.client.post(reverse("nutrition:item_delete", args=[item.pk])).status_code, 404)
        self.login("manager")
        self.assertEqual(len(self.client.get(reverse("nutrition:plan_list")).context["object_list"]), 2)
        for role in ("receptionist", "accountant", "member"):
            self.login(role)
            self.assertEqual(self.client.get(reverse("nutrition:plan_list")).status_code, 403, role)
            self.assertEqual(self.client.get(mine.get_absolute_url()).status_code, 403, role)

    def test_profile_edit_needs_manage_permission(self):
        self.login("trainer")
        url = reverse("nutrition:profile", args=[self.member.pk])
        self.assertEqual(self.client.post(url, {"food_preferences": "Vegan", "allergies": "Nuts", "nutrition_goals": "More fibre"}).status_code, 302)
        p = NutritionProfile.objects.get()
        self.assertEqual((p.allergies, p.updated_by), ("Nuts", self.users["trainer"]))
        self.assertContains(self.client.get(url), "Nuts")


@override_settings(NUTRITION_ENABLED=True)
class PortalNutritionTests(Base):
    def test_member_sees_only_own_plans_and_edits_own_profile(self):
        mine = self.plan(name="My plan")
        MealItem.objects.create(plan=mine, weekday=1, meal_type="dinner", title="Soup")
        self.plan(self.other, name="Private plan")
        self.login(user=self.member_login)
        r = self.client.get(reverse("portal:nutrition"))
        self.assertContains(r, "My plan")
        self.assertContains(r, "Soup")
        self.assertContains(r, "not medical advice")
        self.assertNotContains(r, "Private plan")
        self.assertEqual(self.client.post(reverse("portal:nutrition"), {"food_preferences": "No pork", "allergies": "", "nutrition_goals": "Hydrate"}).status_code, 302)
        self.assertEqual(NutritionProfile.objects.get(member=self.member).food_preferences, "No pork")
        self.assertFalse(NutritionProfile.objects.filter(member=self.other).exists())

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from apps.trainers.models import WEEKDAYS

DISCLAIMER = (
    "General nutrition guidance only – it is not medical advice or a diagnosis. Check with a doctor or registered "
    "dietitian before changing your diet, especially if you have a medical condition, allergies or are pregnant."
)


class NutritionProfile(models.Model):
    """Preferences, allergies and general goals for one member (sensitive: visible only to authorised staff and the member)."""

    member = models.OneToOneField("members.Member", on_delete=models.CASCADE, related_name="nutrition_profile")
    food_preferences = models.TextField(blank=True)
    allergies = models.TextField(blank=True, help_text="Foods to avoid for allergy or intolerance reasons.")
    nutrition_goals = models.TextField(blank=True, help_text="General goals, e.g. eat more vegetables, drink more water.")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class MealPlan(models.Model):
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="meal_plans")
    name = models.CharField(max_length=120)
    goal = models.CharField(max_length=255, blank=True, help_text="General goal of this plan (not a medical target).")
    notes = models.TextField(blank=True)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")), name="mealplan_end_after_start")]

    def __str__(self):
        return f"{self.name} ({self.member})"

    def get_absolute_url(self):
        return reverse("nutrition:plan_detail", args=[self.pk])


class MealType(models.TextChoices):
    BREAKFAST = "breakfast", "Breakfast"
    SNACK_AM = "snack_am", "Morning snack"
    LUNCH = "lunch", "Lunch"
    SNACK_PM = "snack_pm", "Afternoon snack"
    DINNER = "dinner", "Dinner"
    PRE_WORKOUT = "pre_workout", "Pre-workout"
    POST_WORKOUT = "post_workout", "Post-workout"


MEAL_ORDER = {v: i for i, (v, _) in enumerate(MealType.choices)}


class MealItem(models.Model):
    plan = models.ForeignKey(MealPlan, on_delete=models.CASCADE, related_name="items")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    meal_type = models.CharField(max_length=14, choices=MealType.choices)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    ingredients = models.TextField(blank=True, help_text="One ingredient per line.")

    class Meta:
        ordering = ["weekday", "id"]

    @property
    def ingredient_list(self):
        return [line.strip() for line in self.ingredients.splitlines() if line.strip()]

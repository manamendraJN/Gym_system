from django import forms

from apps.accounts.forms import StyledFormMixin
from apps.workouts.forms import resolve_member

from .models import MealItem, MealPlan, NutritionProfile

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = NutritionProfile
        fields = ["food_preferences", "allergies", "nutrition_goals"]
        widgets = {f: forms.Textarea(attrs={"rows": 3}) for f in fields}


class MealPlanForm(StyledFormMixin, forms.ModelForm):
    member = forms.CharField(label="Member ID or phone number", max_length=30)

    class Meta:
        model = MealPlan
        fields = ["name", "goal", "notes", "start_date", "end_date", "is_active"]
        widgets = {"start_date": DATE, "end_date": DATE, "notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_active": "Plan is active"}

    def __init__(self, *args, fixed_member=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fixed_member is not None:
            del self.fields["member"]
        else:
            self.order_fields(["name", "member"])

    def clean_member(self):
        return resolve_member(self.cleaned_data["member"])

    def clean(self):
        c = super().clean()
        if c.get("start_date") and c.get("end_date") and c["end_date"] < c["start_date"]:
            self.add_error("end_date", "The end date cannot be before the start date.")
        return c


class MealItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = MealItem
        fields = ["weekday", "meal_type", "title", "description", "ingredients"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2}), "ingredients": forms.Textarea(attrs={"rows": 4})}

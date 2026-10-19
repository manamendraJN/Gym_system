from decimal import Decimal

from django import forms
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin
from apps.core.validators import validate_image_upload
from apps.members.models import Member, normalize_phone
from apps.trainers.models import Trainer

from .models import Exercise, PlanExercise, WorkoutPlan

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def resolve_member(value):
    value = value.strip()
    member = Member.objects.filter(member_id__iexact=value).first()
    if member is None and normalize_phone(value).lstrip("+"):
        member = Member.objects.filter(phone=normalize_phone(value)).first()
    if member is None:
        raise forms.ValidationError("No member found with that ID or phone number.")
    return member


class ExerciseForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Exercise
        fields = ["name", "muscle_group", "category", "description", "instructions", "image", "video_url", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2}), "instructions": forms.Textarea(attrs={"rows": 5}),
                   "image": forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp"})}
        help_texts = {"image": "JPEG, PNG or WebP, up to 2 MB."}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Exercise.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("An exercise with this name already exists.")
        return name

    def clean_image(self):
        return validate_image_upload(self.cleaned_data.get("image"))

    def clean_video_url(self):
        url = self.cleaned_data["video_url"].strip()
        if url and not url.lower().startswith("https://"):
            raise forms.ValidationError("Use a secure https:// link.")
        return url


class PlanForm(StyledFormMixin, forms.ModelForm):
    member = forms.CharField(label="Member ID or phone number", max_length=30)
    trainer = forms.ModelChoiceField(queryset=Trainer.objects.none(), required=False)

    class Meta:
        model = WorkoutPlan
        fields = ["name", "goal", "description", "start_date", "end_date"]
        widgets = {"start_date": DATE, "end_date": DATE, "description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, fixed_member=None, trainer_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_member = fixed_member
        self.field_order = ["name", "member", "trainer", "goal", "description", "start_date", "end_date"]
        self.order_fields(self.field_order)
        if fixed_member is not None:  # editing: the plan stays with its member
            del self.fields["member"]
            del self.fields["trainer"]
        else:
            self.fields["trainer"].queryset = trainer_choices if trainer_choices is not None else Trainer.objects.exclude(employment_status="terminated")

    def clean_member(self):
        return resolve_member(self.cleaned_data["member"])

    def clean(self):
        c = super().clean()
        if c.get("start_date") and c.get("end_date") and c["end_date"] < c["start_date"]:
            self.add_error("end_date", "The end date cannot be before the start date.")
        return c


class PlanItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PlanExercise
        fields = ["weekday", "exercise", "position", "sets", "reps", "weight_kg", "duration_minutes", "rest_seconds", "notes"]
        widgets = {"weight_kg": forms.NumberInput(attrs={"step": "0.25", "min": "0"})}
        labels = {"weight_kg": "Weight (kg)", "duration_minutes": "Duration (min)", "rest_seconds": "Rest between sets (seconds)",
                  "position": "Order on this day (1 = first)", "sets": "Number of sets", "reps": "Target repetitions per set"}
        help_texts = {"rest_seconds": "Counted down after each set, e.g. 60 = 1 minute, 90 = 1 min 30 s. Leave empty for no rest.",
                      "position": "Exercises run in this order. Leave empty to add it last."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["position"].required = False
        active = Exercise.objects.filter(is_active=True)
        if self.instance.pk:
            active = Exercise.objects.filter(is_active=True) | Exercise.objects.filter(pk=self.instance.exercise_id)
        self.fields["exercise"].queryset = active.order_by("name")

    def clean_position(self):
        value = self.cleaned_data.get("position")
        if value is None and self.instance.pk:
            return self.instance.position  # editing without touching the order keeps it
        return value

    def clean(self):
        c = super().clean()
        if not c.get("reps") and not c.get("duration_minutes"):
            raise forms.ValidationError("Give either repetitions or a duration.")
        for field, hi in (("sets", 50), ("reps", 500), ("duration_minutes", 600), ("rest_seconds", 3600)):
            if c.get(field) is not None and c[field] > hi:
                self.add_error(field, f"At most {hi}.")
        if c.get("sets") is not None and c["sets"] < 1:
            self.add_error("sets", "At least 1 set.")
        if c.get("weight_kg") is not None and not (Decimal("0") <= c["weight_kg"] <= Decimal("1000")):
            self.add_error("weight_kg", "Enter a weight between 0 and 1000 kg.")
        return c


class CopyPlanForm(StyledFormMixin, forms.Form):
    member = forms.CharField(label="Copy to member (ID or phone)", max_length=30)
    start_date = forms.DateField(widget=DATE, initial=timezone.localdate)

    def clean_member(self):
        return resolve_member(self.cleaned_data["member"])


class LogWorkoutForm(StyledFormMixin, forms.Form):
    """One row per active plan exercise: tick what was done and adjust the actual numbers."""

    performed_on = forms.DateField(widget=DATE, label="Date")
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, plan, items, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan, self.items = plan, list(items)
        self.fields["performed_on"].initial = timezone.localdate()
        self.rows = []
        for it in self.items:
            row = {
                "item": it,
                "done": forms.BooleanField(required=False),
                "sets": forms.IntegerField(required=False, min_value=0, max_value=50, initial=it.sets),
                "reps": forms.IntegerField(required=False, min_value=0, max_value=500, initial=it.reps),
                "weight": forms.DecimalField(required=False, min_value=0, max_value=1000, max_digits=6, decimal_places=2, initial=it.weight_kg),
                "duration": forms.IntegerField(required=False, min_value=0, max_value=600, initial=it.duration_minutes),
            }
            for key in ("done", "sets", "reps", "weight", "duration"):
                self.fields[f"{key}_{it.pk}"] = row[key]
            self.rows.append({"item": it, **{k: self[f"{k}_{it.pk}"] for k in ("done", "sets", "reps", "weight", "duration")}})
        for name, f in self.fields.items():
            if name.startswith(("sets_", "reps_", "weight_", "duration_")):
                f.widget.attrs.update({"class": "form-control form-control-sm", "style": "width:90px"})
            elif name.startswith("done_"):
                f.widget.attrs["class"] = "form-check-input"

    def clean_performed_on(self):
        day = self.cleaned_data["performed_on"]
        if day > timezone.localdate():
            raise forms.ValidationError("A workout cannot be recorded in the future.")
        if day < self.plan.start_date:
            raise forms.ValidationError("The date is before this plan started.")
        return day

    def clean(self):
        c = super().clean()
        if not any(c.get(f"done_{it.pk}") for it in self.items):
            raise forms.ValidationError("Tick at least one exercise you completed.")
        return c

    def entries(self):
        c = self.cleaned_data
        return [
            {"item": it, "sets": c.get(f"sets_{it.pk}") or 0, "reps": c.get(f"reps_{it.pk}"),
             "weight": c.get(f"weight_{it.pk}"), "duration": c.get(f"duration_{it.pk}")}
            for it in self.items if c.get(f"done_{it.pk}")
        ]

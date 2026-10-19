from decimal import Decimal

from django import forms
from django.utils import timezone

from apps.accounts.forms import StyledFormMixin

from .models import MeasurementType, ProgressRecord


class MeasurementTypeForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = MeasurementType
        fields = ["name", "unit", "position", "is_active"]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if MeasurementType.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A measurement with this name already exists.")
        return name


class ProgressForm(StyledFormMixin, forms.Form):
    recorded_on = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    weight_kg = forms.DecimalField(label="Body weight (kg)", required=False, max_digits=5, decimal_places=1,
                                   min_value=Decimal("20"), max_value=Decimal("400"), widget=forms.NumberInput(attrs={"step": "0.1"}))
    height_cm = forms.DecimalField(label="Height (cm)", required=False, max_digits=4, decimal_places=1,
                                   min_value=Decimal("100"), max_value=Decimal("250"), widget=forms.NumberInput(attrs={"step": "0.1"}))
    notes = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recorded_on"].initial = timezone.localdate()
        self.types = list(MeasurementType.objects.filter(is_active=True))
        for t in self.types:
            self.fields[f"m_{t.pk}"] = forms.DecimalField(
                label=f"{t.name} ({t.unit})", required=False, max_digits=6, decimal_places=1,
                min_value=Decimal("0.1"), max_value=Decimal("1000"), widget=forms.NumberInput(attrs={"step": "0.1", "class": "form-control"}))
        self._style_fields()

    def clean_recorded_on(self):
        day = self.cleaned_data["recorded_on"]
        if day > timezone.localdate():
            raise forms.ValidationError("The date cannot be in the future.")
        if day < timezone.localdate().replace(year=timezone.localdate().year - 5):
            raise forms.ValidationError("The date is too far in the past.")
        return day

    def clean(self):
        c = super().clean()
        has_number = c.get("weight_kg") or c.get("height_cm") or any(c.get(f"m_{t.pk}") for t in self.types)
        if not has_number:
            raise forms.ValidationError("Enter at least one measurement (weight, height or another value).")
        return c

    def values(self):
        return {t: self.cleaned_data[f"m_{t.pk}"] for t in self.types if self.cleaned_data.get(f"m_{t.pk}")}

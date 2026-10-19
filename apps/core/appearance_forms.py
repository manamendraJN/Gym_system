from django import forms

from apps.accounts.forms import StyledFormMixin

from . import appearance as ap
from .models import Appearance, GymSettings
from .validators import validate_branding_image

PRESET_CHOICES = [(key, p["label"]) for key, p in ap.PRESETS.items()] + [("custom", "Custom")]
IMAGE_FIELDS = ("logo", "favicon", "login_background")


class GymIdentityForm(StyledFormMixin, forms.ModelForm):
    """The gym's displayed name and tagline (stored with the other gym details, see Gym settings)."""

    class Meta:
        model = GymSettings
        fields = ["name", "tagline"]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Enter the gym's name.")
        return name


class AppearanceForm(StyledFormMixin, forms.ModelForm):
    theme_preset = forms.ChoiceField(choices=PRESET_CHOICES)
    remove_logo = forms.BooleanField(required=False, label="Remove the current logo")
    remove_favicon = forms.BooleanField(required=False, label="Remove the current favicon")
    remove_login_background = forms.BooleanField(required=False, label="Remove the current background image")

    class Meta:
        model = Appearance
        fields = ["logo", "favicon", "login_background", "login_title", "login_message", "theme_preset", "mode",
                  *[f"{c}_color" for c in ap.COLOR_FIELDS],
                  "sidebar_style", "sidebar_width", "density", "font_scale", "font_family", "stat_columns"]
        widgets = {name: forms.FileInput(attrs={"accept": "image/png,image/jpeg,image/webp,image/x-icon"}) for name in IMAGE_FIELDS}
        labels = {"stat_columns": "Dashboard tiles", "font_scale": "Text size", "font_family": "Font", "density": "Spacing"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        preset = ap.PRESETS.get(self.instance.theme_preset, ap.PRESETS[ap.DEFAULT_PRESET])
        for c in ap.COLOR_FIELDS:
            field = self.fields[f"{c}_color"]
            field.required = False
            field.widget = forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color", "title": c.title()})
            if not self.initial.get(f"{c}_color"):
                self.initial[f"{c}_color"] = preset[c]
        self.fields["theme_preset"].widget.attrs["class"] = "form-select"
        self.fields["favicon"].widget.attrs["accept"] = "image/png,image/x-icon"

    def _clean_image(self, name):
        return validate_branding_image(self.cleaned_data.get(name), name)

    def clean_logo(self):
        return self._clean_image("logo")

    def clean_favicon(self):
        return self._clean_image("favicon")

    def clean_login_background(self):
        return self._clean_image("login_background")

    def clean(self):
        c = super().clean()
        values = {"theme_preset": c.get("theme_preset"), **{f"{n}_color": c.get(f"{n}_color") for n in ap.COLOR_FIELDS}}
        for n in ap.COLOR_FIELDS:
            if c.get(f"{n}_color") and not ap.HEX.match(c[f"{n}_color"]):
                self.add_error(f"{n}_color", "Enter a colour such as #0f766e.")
        pal = ap.resolve(values)
        if not self.errors:
            if ap.contrast(pal["text"], pal["background"]) < 4.5 or ap.contrast(pal["text"], pal["surface"]) < 4.5:
                self.add_error("text_color", "Text is hard to read on the background/surface colours. Choose a darker text colour or lighter background (contrast of at least 4.5:1).")
            if ap.contrast(pal["primary"], "#ffffff") < 4.5:
                self.add_error("primary_color", "White button text would be hard to read on this colour. Choose a darker primary colour (contrast of at least 4.5:1).")
            if c.get("sidebar_style") == "dark" and ap.contrast(pal["secondary"], "#ffffff") < 7:
                self.add_error("secondary_color", "The dark sidebar needs a darker secondary colour so its white text stays readable.")
        return c

    def save(self, commit=True):
        obj = super().save(commit=False)
        for name in IMAGE_FIELDS:
            if self.cleaned_data.get(f"remove_{name}") and not self.files.get(name):
                obj._to_delete = getattr(obj, "_to_delete", []) + [getattr(self.instance, name)]
                setattr(obj, name, "")
        if commit:
            obj.save()
        return obj

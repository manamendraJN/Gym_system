from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.mixins import GymPermissionMixin

from . import appearance as ap
from .appearance_forms import IMAGE_FIELDS, AppearanceForm, GymIdentityForm
from .gym import gym
from .models import ActivityLog, Appearance, GymSettings

PERMISSION = "settings.appearance"


def _delete_file(field_file):
    """Remove an uploaded file that is no longer referenced (never raises)."""
    try:
        if field_file and field_file.name:
            field_file.storage.delete(field_file.name)
    except Exception:
        pass


class AppearanceView(GymPermissionMixin, TemplateView):
    """System customisation: branding, theme and layout, with a live preview. Global settings: restricted by permission."""

    required_permission = PERMISSION
    template_name = "core/appearance.html"

    def _forms(self, data=None, files=None):
        current = ap.appearance()
        identity = gym()
        gym_instance = identity if isinstance(identity, GymSettings) else GymSettings(pk=1, name=identity.name)
        return (AppearanceForm(data, files, instance=current), GymIdentityForm(data, instance=gym_instance, prefix="gym"))

    def get_context_data(self, **kwargs):
        form, identity = kwargs.pop("forms", None) or self._forms()
        return super().get_context_data(
            title="System customization", form=form, identity=identity, current=form.instance, presets=ap.PRESETS,
            preset_json={k: {c: p[c] for c in ap.COLOR_FIELDS} for k, p in ap.PRESETS.items()}, **kwargs)

    def post(self, request, *args, **kwargs):
        form, identity = self._forms(request.POST, request.FILES)
        old_files = {name: getattr(form.instance, name) for name in IMAGE_FIELDS}
        if not (form.is_valid() & identity.is_valid()):
            messages.error(request, "Nothing was saved. Please fix the highlighted problems.")
            return self.render_to_response(self.get_context_data(forms=(form, identity)), status=422)
        obj = form.save(commit=False)
        obj.pk, obj.updated_by = 1, request.user
        for name in IMAGE_FIELDS:  # an unchanged image field keeps its file; an explicit removal clears it
            if form.cleaned_data.get(f"remove_{name}") and not request.FILES.get(name):
                setattr(obj, name, "")
        obj.save()
        for name, old in old_files.items():
            if old and old.name != getattr(obj, name).name:
                _delete_file(old)
        gym_obj = identity.save(commit=False)
        gym_obj.pk, gym_obj.updated_by = 1, request.user
        gym_obj.save()
        ActivityLog.record(request.user, "appearance_updated", f"Updated system customization (theme: {obj.theme_preset}, mode: {obj.mode})")
        messages.success(request, "Customization saved. It now applies across the system.")
        return redirect("core:appearance")


class AppearancePreviewView(GymPermissionMixin, View):
    """CSS for the unsaved values in the form, so the preview uses exactly the same code as the live system. Saves nothing."""

    required_permission = PERMISSION
    http_method_names = ["post"]

    def post(self, request):
        values = {k: request.POST.get(k, "") for k in ["theme_preset", "mode", *[f"{c}_color" for c in ap.COLOR_FIELDS], *ap.LAYOUT_DEFAULTS]}
        mode = request.POST.get("resolved_mode")
        response = JsonResponse({"css": ap.build_css(values, "#theme-preview", mode if mode in ("light", "dark") else "light")})
        response["Cache-Control"] = "no-store"
        return response


class AppearanceResetView(GymPermissionMixin, View):
    required_permission = PERMISSION
    http_method_names = ["post"]

    def post(self, request):
        obj = ap.reset_theme_and_layout(ap.appearance())
        obj.pk, obj.updated_by = 1, request.user
        obj.save()
        ActivityLog.record(request.user, "appearance_reset", "Reset theme and layout to the defaults")
        messages.success(request, "Theme and layout were reset to the defaults. Your logo, images, texts and gym name were kept.")
        return redirect("core:appearance")

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    BaseUserCreationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
)

from .models import Role

User = get_user_model()


class StyledFormMixin:
    """Adds Bootstrap classes to every widget."""

    def _style_fields(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs["class"] = "form-check-input"
            elif isinstance(widget, forms.Select):
                widget.attrs["class"] = "form-select"
            else:
                widget.attrs["class"] = "form-control"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class LoginForm(StyledFormMixin, AuthenticationForm):
    error_messages = {
        "invalid_login": "Invalid username or password.",
        "inactive": "Invalid username or password.",  # do not reveal that the account exists
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autofocus": True, "placeholder": "Username"})
        self.fields["password"].widget.attrs.update({"placeholder": "Password"})


class StyledPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class StyledPasswordResetForm(StyledFormMixin, PasswordResetForm):
    pass


class StyledSetPasswordForm(StyledFormMixin, SetPasswordForm):
    pass


class ProfileForm(StyledFormMixin, forms.ModelForm):
    """What any signed-in user may change about their own account (never role/status)."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This email address is already in use.")
        return email


class _RoleRestrictedMixin:
    """Limits the selectable roles to those the acting user may assign."""

    def _restrict_roles(self, actor):
        allowed = set(actor.manageable_roles())
        self.fields["role"].choices = [(v, l) for v, l in Role.choices if v in allowed]

    def clean_role(self):
        role = self.cleaned_data["role"]
        if role not in self.actor.manageable_roles():
            raise forms.ValidationError("You are not allowed to assign this role.")
        return role

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This email address is already in use.")
        return email


class UserCreateForm(_RoleRestrictedMixin, StyledFormMixin, BaseUserCreationForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone", "role", "is_active"]

    def __init__(self, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self._restrict_roles(actor)
        self.fields["email"].required = True
        self.fields["is_active"].initial = True
        self.fields["is_active"].label = "Account is active"


class UserUpdateForm(_RoleRestrictedMixin, StyledFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone", "role", "is_active"]

    def __init__(self, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self._restrict_roles(actor)
        self.fields["is_active"].label = "Account is active"
        if self.instance.pk == actor.pk:
            # Nobody can change their own role or lock themselves out.
            for name in ("role", "is_active"):
                self.fields[name].disabled = True
            self.fields["role"].choices = Role.choices

    def clean_role(self):
        if self.instance.pk == self.actor.pk:
            return self.instance.role
        return super().clean_role()

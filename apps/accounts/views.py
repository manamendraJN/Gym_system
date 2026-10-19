from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.core.models import ActivityLog

from . import forms, throttle
from .mixins import GymPermissionMixin
from .models import Role, RolePermission, User
from .permissions import PERMISSION_GROUPS, PERMISSIONS


# --------------------------------------------------------------------------- authentication
@method_decorator(login_not_required, name="dispatch")
class GymLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    authentication_form = forms.LoginForm
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if throttle.is_locked(username, throttle.client_ip(request)):
            form = self.get_form()
            form.add_error(None, "Too many failed attempts. Please try again in a few minutes.")
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        throttle.reset(form.cleaned_data["username"], throttle.client_ip(self.request))
        return super().form_valid(form)

    def form_invalid(self, form):
        throttle.register_failure(self.request.POST.get("username", ""), throttle.client_ip(self.request))
        return super().form_invalid(form)


class GymPasswordChangeView(auth_views.PasswordChangeView):
    template_name = "registration/password_change.html"
    form_class = forms.StyledPasswordChangeForm
    success_url = reverse_lazy("accounts:profile")

    def form_valid(self, form):
        messages.success(self.request, "Your password was changed.")
        ActivityLog.record(self.request.user, "password_change", "Changed their password")
        return super().form_valid(form)


@method_decorator(login_not_required, name="dispatch")
class GymPasswordResetView(auth_views.PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"
    form_class = forms.StyledPasswordResetForm
    extra_email_context = {"GYM_NAME": settings.GYM_NAME}
    success_url = reverse_lazy("accounts:password_reset_done")


@method_decorator(login_not_required, name="dispatch")
class GymPasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"


@method_decorator(login_not_required, name="dispatch")
class GymPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    form_class = forms.StyledSetPasswordForm
    success_url = reverse_lazy("accounts:password_reset_complete")


@method_decorator(login_not_required, name="dispatch")
class GymPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"


# --------------------------------------------------------------------------- own profile
class ProfileView(UpdateView):
    """Every signed-in user (including members) can view and edit only their own details."""

    form_class = forms.ProfileForm
    template_name = "accounts/profile.html"
    success_url = reverse_lazy("accounts:profile")

    def get_object(self, queryset=None):
        return self.request.user

    def form_valid(self, form):
        messages.success(self.request, "Your profile was updated.")
        return super().form_valid(form)


# --------------------------------------------------------------------------- user management
class UserListView(GymPermissionMixin, ListView):
    required_permission = "users.view"
    model = User
    template_name = "accounts/user_list.html"
    paginate_by = 15

    def get_queryset(self):
        qs = User.objects.all()
        q = self.request.GET.get("q", "").strip()
        role = self.request.GET.get("role", "")
        status = self.request.GET.get("status", "")
        if q:
            qs = qs.filter(
                Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                | Q(email__icontains=q)
            )
        if role in Role.values:
            qs = qs.filter(role=role)
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        manageable = set(self.request.user.manageable_roles())
        for u in ctx["object_list"]:
            u.editable = u.role in manageable and u.pk != self.request.user.pk
        ctx.update(
            roles=Role.choices,
            q=self.request.GET.get("q", ""),
            role=self.request.GET.get("role", ""),
            status=self.request.GET.get("status", ""),
            can_manage=self.request.user.has_gym_perm("users.manage"),
        )
        return ctx


class UserCreateView(GymPermissionMixin, CreateView):
    required_permission = "users.manage"
    model = User
    form_class = forms.UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        return super().get_context_data(title="Add user", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        ActivityLog.record(
            self.request.user, "user_created", f"Created {self.object.get_role_display()} account “{self.object.username}”"
        )
        messages.success(self.request, f"Account “{self.object.username}” was created.")
        return response


class UserUpdateView(GymPermissionMixin, UpdateView):
    required_permission = "users.manage"
    model = User
    form_class = forms.UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        # Editing yourself is allowed (role/status are locked in the form); others need a manageable role.
        if obj.pk != self.request.user.pk and not self.request.user.can_manage(obj):
            raise PermissionDenied
        return obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        return super().get_context_data(title=f"Edit {self.object.username}", **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        ActivityLog.record(self.request.user, "user_updated", f"Updated account “{self.object.username}”")
        messages.success(self.request, f"Account “{self.object.username}” was updated.")
        return response


class UserToggleActiveView(GymPermissionMixin, View):
    """Activate / deactivate an account (POST only)."""

    required_permission = "users.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        target = get_object_or_404(User, pk=pk)
        if target.pk == request.user.pk:
            messages.error(request, "You cannot deactivate your own account.")
        elif not request.user.can_manage(target):
            raise PermissionDenied
        else:
            target.is_active = not target.is_active
            target.save(update_fields=["is_active"])
            state = "activated" if target.is_active else "deactivated"
            ActivityLog.record(request.user, f"user_{state}", f"{state.capitalize()} account “{target.username}”")
            messages.success(request, f"Account “{target.username}” was {state}.")
        next_url = request.POST.get("next", "")
        if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect("accounts:user_list")


# --------------------------------------------------------------------------- permission management
class RolePermissionsView(GymPermissionMixin, View):
    """Super Admin only: edit which permissions each role holds."""

    required_permission = "permissions.manage"
    template_name = "accounts/permissions.html"
    editable_roles = [r for r in Role if r != Role.SUPER_ADMIN]

    def _context(self):
        granted = set(RolePermission.objects.values_list("role", "permission"))
        groups = [
            {
                "name": name,
                "perms": [
                    {
                        "code": code,
                        "label": PERMISSIONS[code],
                        "cells": [
                            {"role": r.value, "label": r.label, "checked": (r.value, code) in granted}
                            for r in self.editable_roles
                        ],
                    }
                    for code in codes
                ],
            }
            for name, codes in PERMISSION_GROUPS
        ]
        return {
            "roles": [{"value": r.value, "label": r.label} for r in self.editable_roles],
            "groups": groups,
            "colspan": len(self.editable_roles) + 1,
        }

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        with transaction.atomic():
            for role in self.editable_roles:
                wanted = set(request.POST.getlist(f"perm_{role.value}")) & set(PERMISSIONS)
                RolePermission.objects.filter(role=role.value).exclude(permission__in=wanted).delete()
                existing = set(RolePermission.objects.filter(role=role.value).values_list("permission", flat=True))
                RolePermission.objects.bulk_create(
                    [RolePermission(role=role.value, permission=p) for p in wanted - existing]
                )
        ActivityLog.record(request.user, "permissions_updated", "Updated role permissions")
        messages.success(request, "Role permissions were saved.")
        return redirect("accounts:permissions")

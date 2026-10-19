from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied


class GymPermissionMixin(AccessMixin):
    """Server-side permission gate. Set `required_permission = "module.action"`."""

    required_permission = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.required_permission and not request.user.has_gym_perm(self.required_permission):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

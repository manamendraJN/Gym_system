from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import RegexValidator
from django.db import models
from django.urls import reverse

from .permissions import PERMISSIONS, SUPER_ADMIN_ONLY

phone_validator = RegexValidator(
    r"^\+?[0-9 ()\-]{7,20}$", "Enter a valid phone number (digits, spaces, +, -, parentheses)."
)


class Role(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super Admin"
    MANAGER = "manager", "Gym Manager"
    RECEPTIONIST = "receptionist", "Receptionist"
    TRAINER = "trainer", "Trainer"
    ACCOUNTANT = "accountant", "Accountant"
    MEMBER = "member", "Member"


STAFF_ROLES = [Role.SUPER_ADMIN, Role.MANAGER, Role.RECEPTIONIST, Role.TRAINER, Role.ACCOUNTANT]
# Roles that non-Super-Admin users (e.g. managers) may create and manage.
DELEGATED_ROLES = [Role.RECEPTIONIST, Role.TRAINER, Role.ACCOUNTANT, Role.MEMBER]


class GymUserManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields["role"] = Role.SUPER_ADMIN
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER, db_index=True)
    phone = models.CharField(max_length=20, blank=True, validators=[phone_validator])

    objects = GymUserManager()
    REQUIRED_FIELDS = ["email"]

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def save(self, *args, **kwargs):
        # The role is the single source of truth for admin-level flags, so a crafted
        # request can never create an is_superuser account with a lower role.
        self.email = self.email.strip().lower()
        is_super = self.role == Role.SUPER_ADMIN
        self.is_superuser = is_super
        self.is_staff = is_super
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("accounts:user_edit", args=[self.pk])

    @property
    def is_super_admin(self):
        return self.role == Role.SUPER_ADMIN

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    def gym_permissions(self):
        """Set of permission codes held by this user (cached on the instance)."""
        if not hasattr(self, "_gym_permissions"):
            if not self.is_active:
                perms = set()
            elif self.is_super_admin:
                perms = set(PERMISSIONS) | SUPER_ADMIN_ONLY
            else:
                perms = set(RolePermission.objects.filter(role=self.role).values_list("permission", flat=True))
            self._gym_permissions = perms
        return self._gym_permissions

    def has_gym_perm(self, code):
        return code in self.gym_permissions()

    def manageable_roles(self):
        """Roles this user may assign to / manage on other accounts (prevents privilege escalation)."""
        if self.is_super_admin:
            return [r.value for r in Role]
        if self.has_gym_perm("users.manage"):
            return [r.value for r in DELEGATED_ROLES]
        return []

    def can_manage(self, other):
        return other.role in self.manageable_roles()


class RolePermission(models.Model):
    role = models.CharField(max_length=20, choices=Role.choices)
    permission = models.CharField(max_length=50)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["role", "permission"], name="unique_role_permission")]
        ordering = ["role", "permission"]

    def __str__(self):
        return f"{self.role}: {self.permission}"

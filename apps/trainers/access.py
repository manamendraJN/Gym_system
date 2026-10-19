"""Who may see which member's training data.

Permissions (e.g. ``workouts.view``) say which *modules* a role may use. For the Trainer role the data is further
limited to the members currently assigned to that trainer; every other staff role with the permission sees all
members. A member can only ever see their own records (handled by the portal, never by these helpers).
"""
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Role


def trainer_of(user):
    try:
        return user.trainer_profile
    except Exception:  # no Trainer row linked to this login
        return None


def is_trainer_role(user):
    return user.is_authenticated and user.role == Role.TRAINER


def scope_members(user, queryset):
    """Members `user` may see training data for (queryset of Member)."""
    if not is_trainer_role(user):
        return queryset
    # Only open assignments count: ending an assignment withdraws the trainer's access straight away.
    return queryset.filter(trainer_assignments__trainer__user=user, trainer_assignments__end_date__isnull=True).distinct()


def can_access_member(user, member):
    from apps.members.models import Member

    return scope_members(user, Member.objects.filter(pk=member.pk)).exists()


def scope_by_member(user, queryset, member_field="member"):
    """Restrict any queryset that has a member foreign key to the members the user may see."""
    if not is_trainer_role(user):
        return queryset
    from apps.members.models import Member

    ids = scope_members(user, Member.objects.all()).values("pk")
    return queryset.filter(**{f"{member_field}__in": ids})


def scope_appointments(user, queryset):
    """Trainers see only their own sessions."""
    return queryset.filter(trainer__user=user) if is_trainer_role(user) else queryset

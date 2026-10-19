"""Factories shared by the test suites (test code only)."""
import itertools
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Role, User
from apps.members.tests import make_member
from apps.memberships import services as membership_services
from apps.memberships.models import MembershipPlan
from apps.trainers.models import Trainer, TrainerAvailability

PASSWORD = "Str0ng-Passw0rd!"
_counter = itertools.count(1)


def make_users():
    return {r: User.objects.create_user(username=r, email=f"{r}@example.com", password=PASSWORD, role=r) for r in Role.values}


def make_trainer(name="Coach Kim", user=None, rate="3000", availability=True, **kwargs):
    n = next(_counter)
    trainer = Trainer.objects.create(full_name=name, phone=f"0712{n:06d}", user=user, hourly_rate=Decimal(rate), **kwargs)
    if availability:
        for weekday in range(7):
            TrainerAvailability.objects.create(trainer=trainer, weekday=weekday, start_time=time(6), end_time=time(20))
    return trainer


def future_slot(days=3, hour=10, minute=0):
    day = timezone.localdate() + timedelta(days=days)
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


def give_membership(member, staff, plan=None):
    plan = plan or MembershipPlan.objects.get_or_create(name="Monthly", defaults={"price": Decimal("5000")})[0]
    return membership_services.assign_membership(member, plan, timezone.localdate(), staff)


def make_member_with_login(users, name="Portal Person", membership=True):
    member = make_member(full_name=name)
    login = User.objects.create_user(username=f"m{member.pk}", email=f"m{member.pk}@example.com", password=PASSWORD, role=Role.MEMBER)
    member.user = login
    member.save(update_fields=["user"])
    if membership:
        give_membership(member, users["manager"])
    return member, login


def assign_to_trainer(user, *members):
    """Give the trainer login `user` an open assignment for each member (trainers only see assigned members)."""
    from apps.trainers.models import TrainerAssignment

    trainer = Trainer.objects.filter(user=user).first() or make_trainer(f"Coach {user.username}", user=user, availability=False)
    for member in members:
        TrainerAssignment.objects.get_or_create(trainer=trainer, member=member, end_date=None)
    return trainer

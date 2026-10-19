"""Sample data for development and testing: loading and clean removal.

Loading runs in one transaction. Every row that exists after loading but not before (in this project's own apps) is
written to `SampleRecord`. Cleanup deletes only rows listed there, and skips anything that real data depends on, so
real members, payments, attendance and accounts are never removed. Existing users are never modified; the Super Admin
is never created, changed or deleted."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.apps import apps
from django.db import transaction
from django.db.models import ProtectedError
from django.db.models.deletion import Collector
from django.utils import timezone

from .demo import DEMO_PASSWORD, seed_demo
from .models import SampleRecord

SAMPLE_STAFF = (  # username, role, first name
    ("demo_manager", "manager", "Demo Manager"),
    ("demo_reception", "receptionist", "Demo Reception"),
    ("demo_accountant", "accountant", "Demo Accountant"),
)


class SampleDataError(Exception):
    pass


def _own_models():
    return [m for m in apps.get_models() if m._meta.app_config.name.startswith("apps.") and m is not SampleRecord and not m._meta.auto_created]


def _snapshot():
    return {m._meta.label: set(m.objects.values_list("pk", flat=True)) for m in _own_models()}


def is_loaded():
    return SampleRecord.objects.exists()


@transaction.atomic
def load_sample_data():
    """Returns a dict describing what was created; raises SampleDataError when nothing should be added."""
    from apps.accounts.models import Role, User
    from apps.attendance import services as attendance
    from apps.attendance.models import Attendance
    from apps.members.models import Member
    from apps.memberships import services as memberships
    from apps.memberships.models import Membership, MembershipPlan
    from apps.payments import services as payments

    if is_loaded():
        raise SampleDataError("Sample data is already loaded. Run cleanup_sample_data first if you want a fresh copy.")
    if Member.objects.filter(full_name__startswith="Demo ").exists() or User.objects.filter(username__startswith="demo_").exists():
        raise SampleDataError("Records named 'Demo ...' / 'demo_...' already exist but were not created by this loader, so they cannot be "
                              "tracked or removed safely. Nothing was added.")
    before = _snapshot()

    # Staff logins (never superusers; existing usernames are left alone).
    staff = {}
    for username, role, first_name in SAMPLE_STAFF:
        staff[username] = User.objects.create_user(username, f"{username}@example.com", DEMO_PASSWORD, role=role, first_name=first_name)
    actor = User.objects.filter(role=Role.SUPER_ADMIN, is_active=True).first() or staff["demo_manager"]

    result = seed_demo(actor)
    if result is None:  # cannot happen after the check above, but never continue half-way
        raise SampleDataError("Demo data already exists.")

    # --- more members, plans in different states, QR + manual attendance history --------------------------------
    today = timezone.localdate()
    monthly = MembershipPlan.objects.get(name="Demo Monthly")
    quarterly = MembershipPlan.objects.get(name="Demo Quarterly")

    def member(n, name):
        return Member.objects.create(full_name=f"Demo {name}", phone=f"07792{n:05d}", email=f"demo.{name.lower()}@example.com",
                                     date_of_birth=today - timedelta(days=365 * 27), emergency_contact_name="Demo Contact",
                                     emergency_contact_phone=f"07793{n:05d}", registration_date=today - timedelta(days=45))

    fiona, george, hana, ian = (member(i, n) for i, n in enumerate(("Fiona", "George", "Hana", "Ian"), start=1))
    ms_fiona = memberships.assign_membership(fiona, quarterly, today, actor)
    payments.record_payment(ms_fiona, ms_fiona.price, "card", actor, "demo-f1")
    ms_george = memberships.assign_membership(george, monthly, today, actor)
    payments.record_payment(ms_george, Decimal("2500"), "cash", actor, "demo-g1")  # partial payment
    Membership.objects.create(member=hana, plan=monthly, start_date=today - timedelta(days=45), end_date=today - timedelta(days=15), price=monthly.price)
    ian.is_active = False  # deactivated member: scans must be refused
    ian.save(update_fields=["is_active"])

    # a week of earlier visits (kept as history, QR and front-desk)
    for who, ms, days in ((result["alice"], result["membership"], (1, 2, 4, 6)), (fiona, ms_fiona, (1, 3, 5)), (george, ms_george, (2, 5))):
        for i, d in enumerate(days):
            start = timezone.make_aware(datetime.combine(today - timedelta(days=d), time(17, 30)))
            Attendance.objects.create(member=who, membership=ms, check_in=start, check_out=start + timedelta(minutes=75 + 10 * i),
                                      method="qr" if i % 2 == 0 else "manual", checked_in_by=actor, checked_out_by=actor)
    attendance.scan_qr(attendance.qr_payload(george), actor)  # George is inside right now, checked in by a real QR scan

    after = _snapshot()
    rows = [SampleRecord(model_label=label, object_pk=str(pk)) for label, pks in after.items() for pk in pks - before.get(label, set())]
    SampleRecord.objects.bulk_create(rows)
    return {"records": len(rows), "staff": [u for u, _, _ in SAMPLE_STAFF], "members": Member.objects.filter(full_name__startswith="Demo ").count()}


def _tracked():
    tracked = {}
    for label, pk in SampleRecord.objects.values_list("model_label", "object_pk"):
        tracked.setdefault(label, set()).add(pk)
    return tracked


def _depends_on_real_data(queryset, tracked):
    """True if deleting `queryset` would also delete (cascade) a row that is not a tracked sample row."""
    collector = Collector(using=queryset.db)
    try:
        collector.collect(list(queryset))
    except ProtectedError:
        return True
    for model, instances in collector.data.items():
        if model._meta.auto_created:
            continue
        ok = tracked.get(model._meta.label, set())
        if any(str(obj.pk) not in ok for obj in instances):
            return True
    for qs in collector.fast_deletes:
        if qs.model._meta.auto_created:
            continue
        ok = tracked.get(qs.model._meta.label, set())
        if any(str(pk) not in ok for pk in qs.values_list("pk", flat=True)):
            return True
    return False


def cleanup_sample_data():
    """Delete only tracked sample rows. Returns (deleted_count, preserved list of 'Model#pk')."""
    from apps.accounts.models import Role

    tracked = _tracked()
    pending = []
    for label, pks in tracked.items():
        model = apps.get_model(label)
        qs = model.objects.filter(pk__in=pks)
        if label == "accounts.User":  # never touch a Super Admin / superuser / staff-flag account, whatever the register says
            qs = qs.exclude(is_superuser=True).exclude(role=Role.SUPER_ADMIN).exclude(is_staff=True)
        pending.append(qs)
    deleted, preserved = 0, []

    def attempt(qs):
        """Delete qs if nothing outside the register depends on it; returns the number deleted, or None if blocked."""
        with transaction.atomic():
            if _depends_on_real_data(qs, tracked):
                return None
            try:
                return qs.delete()[0]
            except ProtectedError:
                transaction.set_rollback(True)
                return None

    # Repeat until nothing more can be deleted: rows that others still reference (PROTECT) go in later rounds,
    # and rows that reference each other (e.g. a refund pointing at its payment) are removed one at a time.
    progress = True
    while progress and pending:
        progress, still = False, []
        for qs in pending:
            if not qs.exists():
                continue
            count = attempt(qs)
            if count is None and qs.count() > 1:
                count = 0
                for pk in list(qs.values_list("pk", flat=True)):
                    one = attempt(qs.filter(pk=pk))
                    count += one or 0
                count = count or None
            if count is None:
                still.append(qs)
                continue
            deleted += count
            progress = True
            if qs.exists():
                still.append(qs)
        pending = still
    for qs in pending:
        preserved.extend(f"{qs.model._meta.label}#{pk}" for pk in qs.values_list("pk", flat=True))
    # forget the register entries of rows that are gone
    for label, pks in tracked.items():
        model = apps.get_model(label)
        gone = pks - {str(p) for p in model.objects.filter(pk__in=pks).values_list("pk", flat=True)}
        SampleRecord.objects.filter(model_label=label, object_pk__in=gone).delete()
    return deleted, preserved

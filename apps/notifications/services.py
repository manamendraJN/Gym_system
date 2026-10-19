"""Scheduled notification generators.

Every generator is safe to run as often as you like: each reminder carries a deterministic `dedupe_key`
(e.g. ``membership-expiring:42:7``) and Notification.send() refuses to create a second notification with the same
key for the same user, so a repeated or overlapping run (cron + Celery, a retry, a restart) never duplicates anything.
Reminder periods come from NotificationSettings and can be changed without code. If a run is missed, the next run
sends the most urgent reminder that is due (earlier ones are not sent late in bulk).
"""
from collections import Counter
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Role, RolePermission, User
from apps.core.models import Notification

from .models import NotificationSettings


def users_with_permission(code):
    """Active users whose role holds `code` (the Super Admin holds everything)."""
    roles = list(RolePermission.objects.filter(permission=code).values_list("role", flat=True))
    return User.objects.filter(is_active=True).filter(Q(role=Role.SUPER_ADMIN) | Q(role__in=roles))


def _band(left, offsets):
    """Smallest configured offset that has been reached (left <= offset), or None. Offsets are the reminder periods."""
    reached = [o for o in offsets if left <= o]
    return min(reached) if reached else None


def _digest(users, key, title, message, url, kind):
    return sum(bool(Notification.send(u, title, message, url, kind=kind, dedupe_key=key)) for u in users)


# ---------------------------------------------------------------------------------------------- memberships
def membership_expiry_reminders(today=None):
    from apps.memberships.queries import expiring_soon

    today = today or timezone.localdate()
    offsets = NotificationSettings.load().expiry_days()
    created = 0
    memberships = list(expiring_soon(max(offsets), today).select_related("member__user", "plan"))
    for m in memberships:
        left = (m.end_date - today).days
        offset = _band(left, offsets)
        if offset is None or not m.member.user_id:
            continue
        when = "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''}"
        created += bool(Notification.send(
            m.member.user, "Membership expiring soon", f"Your {m.plan.name} membership ends {when} ({m.end_date:%b %d, %Y}). Renew to keep your access.",
            "/me/", kind="membership-expiring", dedupe_key=f"membership-expiring:{m.pk}:{offset}", external=True))
    if memberships:
        created += _digest(users_with_permission("memberships.manage"), f"staff-expiring-digest:{today}", "Memberships expiring soon",
                           f"{len(memberships)} membership(s) end within {max(offsets)} days and have not been renewed.", "/memberships/expiring/", "membership-expiring")
    return created


def membership_expired_notices(today=None, lookback_days=7):
    from apps.memberships.queries import expired_memberships

    today = today or timezone.localdate()
    recent = list(expired_memberships(today).filter(end_date__gte=today - timedelta(days=lookback_days), member__is_active=True)
                  .select_related("member__user", "plan"))
    created = 0
    for m in recent:
        if m.member.user_id:
            created += bool(Notification.send(
                m.member.user, "Membership expired", f"Your {m.plan.name} membership ended on {m.end_date:%b %d, %Y}. Renew to continue training.",
                "/me/", kind="membership-expired", dedupe_key=f"membership-expired:{m.pk}", external=True))
    if recent:
        created += _digest(users_with_permission("memberships.manage"), f"staff-expired-digest:{today}", "Memberships expired",
                           f"{len(recent)} membership(s) expired in the last {lookback_days} days.", "/memberships/expired/", "membership-expired")
    return created


def payment_due_reminders(today=None):
    """A membership with an unpaid balance is due `payment_due_grace_days` after it starts."""
    from apps.memberships.models import Membership
    from apps.payments.billing import with_billing

    today = today or timezone.localdate()
    conf = NotificationSettings.load()
    offsets = conf.due_offsets()
    billed = with_billing(Membership.objects.filter(member__is_active=True).select_related("member__user", "plan")).filter(outstanding__gt=0)
    created, overdue = 0, 0
    for m in billed:
        due = m.start_date + timedelta(days=conf.payment_due_grace_days)
        left = (due - today).days
        if left <= 0:
            overdue += 1
        offset = _band(left, offsets)
        if offset is None or not m.member.user_id:
            continue
        if left > 0:
            text = f"Payment of LKR {m.outstanding:,.2f} for your {m.plan.name} membership is due on {due:%b %d, %Y}."
        elif left == 0:
            text = f"Payment of LKR {m.outstanding:,.2f} for your {m.plan.name} membership is due today."
        else:
            text = f"Payment of LKR {m.outstanding:,.2f} for your {m.plan.name} membership is overdue (was due {due:%b %d, %Y})."
        created += bool(Notification.send(m.member.user, "Payment due" if left >= 0 else "Payment overdue", text, "/me/",
                                          kind="payment-due", dedupe_key=f"payment-due:{m.pk}:{offset}", external=True))
    if overdue:
        week = today.isocalendar()
        created += _digest(users_with_permission("payments.manage"), f"staff-payment-digest:{week[0]}-W{week[1]}", "Payments due / overdue",
                           f"{overdue} membership(s) have an unpaid balance past their due date.", "/payments/outstanding/", "payment-due")
    return created


# ------------------------------------------------------------------------------------ equipment & inventory
def equipment_maintenance_reminders(today=None):
    from apps.equipment.models import Equipment

    today = today or timezone.localdate()
    offsets = NotificationSettings.load().maintenance_days()
    staff = list(users_with_permission("equipment.manage"))
    created = 0
    for eq in Equipment.objects.filter(next_maintenance_date__isnull=False, next_maintenance_date__lte=today + timedelta(days=max(offsets))):
        left = (eq.next_maintenance_date - today).days
        offset = _band(left, offsets)
        if offset is None:
            continue
        if left < 0:
            text = f"{eq.code} {eq.name}: maintenance was due on {eq.next_maintenance_date:%b %d, %Y} ({-left} day(s) overdue)."
        elif left == 0:
            text = f"{eq.code} {eq.name}: maintenance is due today."
        else:
            text = f"{eq.code} {eq.name}: maintenance is due on {eq.next_maintenance_date:%b %d, %Y} (in {left} day(s))."
        for user in staff:
            # the due date is part of the key, so completing a service and scheduling the next one starts a fresh cycle
            created += bool(Notification.send(user, "Equipment maintenance due", text, eq.get_absolute_url(), kind="equipment-maintenance",
                                              dedupe_key=f"maintenance-due:{eq.pk}:{eq.next_maintenance_date}:{offset}"))
    return created


def low_stock_alerts():
    from django.conf import settings

    if not settings.INVENTORY_ENABLED:
        return 0
    from apps.inventory.models import InventoryItem
    from apps.inventory.services import notify_if_low

    return sum(notify_if_low(item) for item in InventoryItem.objects.filter(is_active=True) if item.is_low)


# -------------------------------------------------------------------------------------------------- run all
def run_all(now=None):
    """Run every scheduled generator, then attempt queued external deliveries. Returns counts for reporting."""
    from apps.announcements.services import publish_due
    from apps.appointments.services import send_due_reminders

    from .delivery import process_queue

    now = now or timezone.now()
    today = timezone.localdate(now)
    results = {
        "announcements": publish_due(now),
        "membership_expiring": membership_expiry_reminders(today),
        "membership_expired": membership_expired_notices(today),
        "payment_due": payment_due_reminders(today),
        "appointments": send_due_reminders(now),
        "equipment": equipment_maintenance_reminders(today),
        "low_stock": low_stock_alerts(),
    }
    results["deliveries"] = process_queue()
    return results

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.core.models import Notification

from .models import Announcement, Audience


def live_announcements(now=None):
    now = now or timezone.now()
    return Announcement.objects.filter(is_active=True, publish_at__lte=now).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def audiences_for(user):
    """Audience codes that include this user."""
    if user.role == Role.MEMBER:
        from apps.members.models import Member
        from apps.memberships.queries import members_with_status

        audiences = {Audience.EVERYONE, Audience.MEMBERS_ALL}
        member = getattr(user, "member_profile", None)
        if member is not None:
            status = members_with_status(Member.objects.filter(pk=member.pk)).get().membership_status
            if status == "active":
                audiences.add(Audience.MEMBERS_ACTIVE)
            elif status == "expired":
                audiences.add(Audience.MEMBERS_EXPIRED)
        return audiences
    audiences = {Audience.EVERYONE, Audience.STAFF}
    if user.role == Role.TRAINER:
        audiences.add(Audience.TRAINERS)
    return audiences


def visible_for(user, now=None):
    """Live announcements addressed to this user."""
    return live_announcements(now).filter(audience__in=audiences_for(user))


def recipients(announcement):
    """Active users in the announcement's audience. Members without a portal login have no account to notify."""
    audience = announcement.audience
    users = User.objects.filter(is_active=True)
    if audience == Audience.EVERYONE:
        return users
    if audience == Audience.STAFF:
        return users.exclude(role=Role.MEMBER)
    if audience == Audience.TRAINERS:
        return users.filter(role=Role.TRAINER)
    from apps.members.models import Member
    from apps.memberships.queries import members_with_status

    members = Member.objects.filter(is_active=True, user__isnull=False)
    if audience in (Audience.MEMBERS_ACTIVE, Audience.MEMBERS_EXPIRED):
        wanted = "active" if audience == Audience.MEMBERS_ACTIVE else "expired"
        members = members_with_status(members).filter(membership_status=wanted)
    return users.filter(pk__in=members.values("user"), role=Role.MEMBER)


def publish_due(now=None):
    """Create notifications for announcements that have reached their publication time. Each announcement is
    processed once (notified_at) and each user can only ever receive it once (dedupe key)."""
    now = now or timezone.now()
    done = 0
    due = live_announcements(now).filter(send_notification=True, notified_at__isnull=True)
    for ann in due:
        created = 0
        for user in recipients(ann):
            note = Notification.send(user, ann.title, ann.body[:255], "/me/" if user.role == Role.MEMBER else "/",
                                     kind="announcement", dedupe_key=f"announcement:{ann.pk}", external=True)
            created += bool(note)
        ann.notified_at, ann.notified_count = now, created
        ann.save(update_fields=["notified_at", "notified_count"])
        done += created
    return done

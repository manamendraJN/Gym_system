from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.models import Notification
from apps.core.testing import PASSWORD, give_membership, make_member_with_login, make_users
from apps.memberships.models import Membership

from . import services
from .models import Announcement


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = make_users()
        self.manager = self.users["manager"]
        self.active, self.active_login = make_member_with_login(self.users, "Active Member")
        self.expired, self.expired_login = make_member_with_login(self.users, "Expired Member", membership=False)
        today = timezone.localdate()
        plan = Membership.objects.first().plan
        Membership.objects.create(member=self.expired, plan=plan, start_date=today - timedelta(days=60), end_date=today - timedelta(days=30), price=plan.price)

    def login(self, user):
        self.client.logout()
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))

    def announce(self, **kw):
        data = dict(title="Holiday hours", body="We close early on Friday.", audience="members_all", created_by=self.manager)
        data.update(kw)
        return Announcement.objects.create(**data)

    def notified(self, ann):
        return set(Notification.objects.filter(dedupe_key=f"announcement:{ann.pk}").values_list("user__username", flat=True))


class AudienceTests(Base):
    def test_recipients_by_audience(self):
        names = lambda aud: set(services.recipients(self.announce(audience=aud)).values_list("username", flat=True))
        self.assertEqual(names("members_all"), {self.active_login.username, self.expired_login.username})
        self.assertEqual(names("members_active"), {self.active_login.username})
        self.assertEqual(names("members_expired"), {self.expired_login.username})
        self.assertEqual(names("trainers"), {"trainer"})
        self.assertEqual(names("staff"), {"super_admin", "manager", "receptionist", "trainer", "accountant"})
        self.assertEqual(len(names("everyone")), 8)  # all six built-in roles' users + the two portal members... member user from make_users included

    def test_inactive_users_are_never_notified(self):
        self.active_login.is_active = False
        self.active_login.save()
        self.assertNotIn(self.active_login.username, set(services.recipients(self.announce()).values_list("username", flat=True)))

    def test_visible_for_matches_the_viewer(self):
        everyone, members, active, expired, staff = (self.announce(title=t, audience=a) for t, a in
            (("E", "everyone"), ("M", "members_all"), ("A", "members_active"), ("X", "members_expired"), ("S", "staff")))
        titles = lambda u: {a.title for a in services.visible_for(u)}
        self.assertEqual(titles(self.active_login), {"E", "M", "A"})
        self.assertEqual(titles(self.expired_login), {"E", "M", "X"})
        self.assertEqual(titles(self.users["receptionist"]), {"E", "S"})
        self.assertEqual(titles(self.users["manager"]), {"E", "S"})


class PublicationTests(Base):
    def test_publishing_notifies_once_and_records_counts(self):
        ann = self.announce(audience="members_active")
        self.assertEqual(services.publish_due(), 1)
        self.assertEqual(self.notified(ann), {self.active_login.username})
        self.assertEqual(services.publish_due(), 0)  # a second scheduler run adds nothing
        ann.refresh_from_db()
        self.assertEqual((ann.notified_count, ann.notified_at is not None), (1, True))

    def test_dedupe_key_blocks_duplicates_even_if_notified_at_is_lost(self):
        ann = self.announce(audience="members_active")
        services.publish_due()
        Announcement.objects.filter(pk=ann.pk).update(notified_at=None)  # simulate a crash before bookkeeping
        self.assertEqual(services.publish_due(), 0)
        self.assertEqual(Notification.objects.filter(dedupe_key=f"announcement:{ann.pk}").count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(user=self.active_login, title="x", message="y", dedupe_key=f"announcement:{ann.pk}")

    def test_scheduled_announcements_wait_for_their_time(self):
        now = timezone.now()
        ann = self.announce(publish_at=now + timedelta(hours=3))
        self.assertEqual(services.publish_due(now), 0)
        self.assertEqual(services.live_announcements(now).count(), 0)
        self.assertEqual(services.publish_due(now + timedelta(hours=4)), 2)

    def test_expired_and_inactive_announcements_do_not_publish(self):
        base = timezone.now()
        self.announce(publish_at=base - timedelta(days=2), expires_at=base - timedelta(days=1))
        self.announce(is_active=False)
        self.announce(send_notification=False)
        now = timezone.now() + timedelta(seconds=1)
        self.assertEqual(services.publish_due(now), 0)
        self.assertEqual(services.live_announcements(now).count(), 1)  # the one without notification is still live on screen

    def test_status_labels(self):
        now = timezone.now()
        self.assertEqual(self.announce(publish_at=now + timedelta(hours=1)).status_key, "scheduled")
        self.assertEqual(self.announce(publish_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)).status_key, "expired")
        self.assertEqual(self.announce(is_active=False).status_key, "inactive")
        self.assertEqual(self.announce().status_key, "live")

    def test_database_rejects_expiry_before_publication(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.announce(publish_at=timezone.now(), expires_at=timezone.now() - timedelta(hours=1))


class ManagementTests(Base):
    def form(self, **kw):
        data = {"title": "Pool closed", "kind": "closure", "audience": "members_all", "body": "The pool is closed on Monday for cleaning.",
                "publish_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "expires_at": "", "send_notification": "on", "is_active": "on"}
        data.update(kw)
        return data

    def test_manager_creates_and_members_are_notified_immediately(self):
        self.login(self.manager)
        r = self.client.post(reverse("announcements:create"), self.form())
        self.assertRedirects(r, reverse("announcements:list"))
        ann = Announcement.objects.get()
        self.assertEqual((ann.kind, ann.created_by), ("closure", self.manager))
        self.assertEqual(self.notified(ann), {self.active_login.username, self.expired_login.username})
        self.login(self.active_login)
        home = self.client.get(reverse("portal:home"))
        self.assertContains(home, "Pool closed")
        self.assertContains(home, "alert-danger")  # closure notices stand out

    def test_validation(self):
        self.login(self.manager)
        later = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        earlier = (timezone.localtime() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.assertContains(self.client.post(reverse("announcements:create"), self.form(publish_at=later, expires_at=earlier)), "after the publication")
        self.assertEqual(self.client.post(reverse("announcements:create"), self.form(title="")).status_code, 200)
        self.assertFalse(Announcement.objects.exists())

    def test_future_announcement_is_scheduled_not_sent(self):
        self.login(self.manager)
        later = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(reverse("announcements:create"), self.form(publish_at=later))
        ann = Announcement.objects.get()
        self.assertEqual((ann.status_key, ann.notified_at), ("scheduled", None))
        self.assertFalse(Notification.objects.filter(kind="announcement").exists())
        self.login(self.active_login)
        self.assertNotContains(self.client.get(reverse("portal:home")), "Pool closed")

    def test_edit_does_not_notify_again_and_deactivate_hides_it(self):
        self.login(self.manager)
        self.client.post(reverse("announcements:create"), self.form())
        ann = Announcement.objects.get()
        count = Notification.objects.filter(kind="announcement").count()
        self.client.post(reverse("announcements:edit", args=[ann.pk]), self.form(title="Pool closed (updated)"))
        ann.refresh_from_db()
        self.assertEqual(ann.title, "Pool closed (updated)")
        self.assertEqual(Notification.objects.filter(kind="announcement").count(), count)
        self.client.post(reverse("announcements:toggle", args=[ann.pk]))
        ann.refresh_from_db()
        self.assertFalse(ann.is_active)
        self.login(self.active_login)
        self.assertNotContains(self.client.get(reverse("portal:home")), "alert-danger")  # banner gone (the earlier notification stays in history)
        self.assertEqual(self.client.get(reverse("announcements:toggle", args=[ann.pk])).status_code, 403)

    def test_status_filter_and_permissions(self):
        live, off = self.announce(title="Live one"), self.announce(title="Off one", is_active=False)
        self.login(self.manager)
        titles = lambda **p: {a.title for a in self.client.get(reverse("announcements:list"), p).context["object_list"]}
        self.assertEqual(titles(status="live"), {"Live one"})
        self.assertEqual(titles(status="inactive"), {"Off one"})
        for role in ("receptionist", "trainer", "accountant", "member"):
            self.login(self.users[role])
            self.assertEqual(self.client.get(reverse("announcements:list")).status_code, 403, role)
            self.assertEqual(self.client.post(reverse("announcements:create"), self.form()).status_code, 403, role)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("announcements:list")).status_code, 302)

    def test_staff_see_staff_announcements_on_the_dashboard(self):
        self.announce(title="Staff meeting", audience="staff")
        self.announce(title="Member offer", audience="members_all")
        self.login(self.users["receptionist"])
        page = self.client.get(reverse("core:home"))
        self.assertContains(page, "Staff meeting")
        self.assertNotContains(page, "Member offer")

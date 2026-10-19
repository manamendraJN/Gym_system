from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, RolePermission, User
from apps.core.stats import get_dashboard_stats
from apps.members.models import Member
from apps.members.tests import make_member

from . import services
from .models import DurationType, Membership, MembershipPlan, add_months
from .queries import expired_memberships, expiring_soon, members_with_status
from .services import MembershipError

PASSWORD = "Str0ng-Passw0rd!"
TODAY = date(2026, 3, 10)


@contextmanager
def on(day):
    """Run code as if today were `day`."""
    with mock.patch("django.utils.timezone.localdate", return_value=day):
        yield


def plan(name="Monthly", months=DurationType.MONTHLY, price="5000", **kw):
    return MembershipPlan.objects.create(name=name, duration_type=months, price=Decimal(price), **kw)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = {
            r: User.objects.create_user(username=r, email=f"{r}@example.com", password=PASSWORD, role=r) for r in Role.values
        }
        self.staff = self.users["manager"]
        self.monthly = plan()
        self.annual = plan("Annual", DurationType.ANNUAL, "50000")
        self.member = make_member(registration_date=TODAY)

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))


class DateCalculationTests(TestCase):
    def test_add_months_clamps_day(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 11, 15), 3), date(2027, 2, 15))
        self.assertEqual(add_months(date(2026, 12, 1), 1), date(2027, 1, 1))

    def test_plan_end_dates(self):
        def end(dtype, start, **kw):
            return MembershipPlan(name="x", duration_type=dtype, price=1, **kw).end_date_for(start)

        self.assertEqual(end(DurationType.MONTHLY, date(2026, 1, 1)), date(2026, 1, 31))
        self.assertEqual(end(DurationType.MONTHLY, date(2026, 1, 15)), date(2026, 2, 14))
        self.assertEqual(end(DurationType.MONTHLY, date(2026, 1, 31)), date(2026, 2, 27))
        self.assertEqual(end(DurationType.QUARTERLY, date(2026, 1, 1)), date(2026, 3, 31))
        self.assertEqual(end(DurationType.SEMIANNUAL, date(2026, 1, 1)), date(2026, 6, 30))
        self.assertEqual(end(DurationType.ANNUAL, date(2026, 1, 1)), date(2026, 12, 31))
        self.assertEqual(end(DurationType.ANNUAL, date(2028, 2, 29)), date(2029, 2, 27))
        self.assertEqual(end(DurationType.CUSTOM, date(2026, 1, 1), custom_days=30), date(2026, 1, 30))
        self.assertEqual(end(DurationType.CUSTOM, date(2026, 1, 1), custom_days=1), date(2026, 1, 1))

    def test_plan_constraints(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            MembershipPlan.objects.create(name="Bad", duration_type="custom", price=1)  # no custom_days
        with self.assertRaises(IntegrityError), transaction.atomic():
            MembershipPlan.objects.create(name="Bad2", duration_type="monthly", custom_days=5, price=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MembershipPlan.objects.create(name="Neg", price=-1)
        plan("Unique")
        with self.assertRaises(IntegrityError), transaction.atomic():
            plan("UNIQUE")


class AssignTests(Base):
    def test_assign_computes_dates_and_snapshots_price(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
        self.assertEqual((m.start_date, m.end_date), (date(2026, 3, 10), date(2026, 4, 9)))
        self.assertEqual(m.price, Decimal("5000"))
        self.assertEqual(m.kind, "new")
        self.monthly.price = Decimal("9999")
        self.monthly.save()
        m.refresh_from_db()
        self.assertEqual(m.price, Decimal("5000"))  # later price edits never change existing memberships

    def test_rules_reject_invalid_assignments(self):
        with on(TODAY):
            with self.assertRaisesMessage(MembershipError, "past"):
                services.assign_membership(self.member, self.monthly, TODAY - timedelta(days=1), self.staff)
            with self.assertRaisesMessage(MembershipError, "ahead"):
                services.assign_membership(self.member, self.monthly, TODAY + timedelta(days=61), self.staff)
            self.monthly.is_active = False
            self.monthly.save()
            with self.assertRaisesMessage(MembershipError, "inactive"):
                services.assign_membership(self.member, self.monthly, TODAY, self.staff)
            services.assign_membership(self.member, self.annual, TODAY, self.staff)
            with self.assertRaisesMessage(MembershipError, "already has"):
                services.assign_membership(self.member, self.annual, TODAY, self.staff)
            self.member.is_active = False
            self.member.save()
            other = make_member()
            other.is_active = False
            other.save()
            with self.assertRaisesMessage(MembershipError, "deactivated"):
                services.assign_membership(other, self.annual, TODAY, self.staff)

    def test_can_assign_again_after_expiry(self):
        with on(TODAY):
            services.assign_membership(self.member, self.monthly, TODAY, self.staff)
        later = TODAY + timedelta(days=60)
        with on(later):
            m2 = services.assign_membership(self.member, self.monthly, later, self.staff)
        self.assertEqual(self.member.memberships.count(), 2)
        self.assertEqual(m2.start_date, later)

    def test_database_rejects_invalid_period(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(member=self.member, plan=self.monthly, start_date=TODAY,
                                      end_date=TODAY - timedelta(days=1), price=1)


class RenewalTests(Base):
    def setUp(self):
        super().setUp()
        with on(TODAY):
            self.first = services.assign_membership(self.member, self.monthly, TODAY, self.staff)  # Mar 10 – Apr 9

    def test_early_renewal_starts_after_current_period_and_keeps_history(self):
        with on(date(2026, 4, 1)):
            r = services.renew_membership(self.first, self.staff)
        self.assertEqual((r.start_date, r.end_date), (date(2026, 4, 10), date(2026, 5, 9)))
        self.assertEqual(r.previous_id, self.first.pk)
        self.assertEqual(r.kind, "renewal")
        self.first.refresh_from_db()
        self.assertEqual((self.first.start_date, self.first.end_date, self.first.status), (TODAY, date(2026, 4, 9), "active"))
        self.assertEqual(r.display_status(date(2026, 4, 1)), "scheduled")

    def test_renewal_on_last_day_is_still_contiguous(self):
        with on(date(2026, 4, 9)):
            r = services.renew_membership(self.first, self.staff)
        self.assertEqual(r.start_date, date(2026, 4, 10))

    def test_late_renewal_starts_today_without_backdating(self):
        with on(date(2026, 4, 20)):
            r = services.renew_membership(self.first, self.staff)
        self.assertEqual((r.start_date, r.end_date), (date(2026, 4, 20), date(2026, 5, 19)))
        self.first.refresh_from_db()
        self.assertEqual(self.first.end_date, date(2026, 4, 9))

    def test_cannot_renew_twice_or_with_inactive_plan(self):
        with on(date(2026, 4, 1)):
            services.renew_membership(self.first, self.staff)
            with self.assertRaisesMessage(MembershipError, "already been renewed"):
                services.renew_membership(self.first, self.staff)
        other = make_member()
        with on(TODAY):
            m = services.assign_membership(other, self.monthly, TODAY, self.staff)
            self.monthly.is_active = False
            self.monthly.save()
            with self.assertRaisesMessage(MembershipError, "inactive"):
                services.renew_membership(m, self.staff)

    def test_renew_onto_different_plan_uses_new_price(self):
        with on(date(2026, 4, 1)):
            r = services.renew_membership(self.first, self.staff, plan=self.annual)
        self.assertEqual(r.price, Decimal("50000"))
        self.assertEqual(r.end_date, date(2027, 4, 9))


class PlanChangeTests(Base):
    def test_upgrade_supersedes_old_period_and_preserves_it(self):
        with on(TODAY):
            old = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
        with on(date(2026, 3, 20)):
            new = services.change_plan(old, self.annual, self.staff)
        old.refresh_from_db()
        self.assertEqual(new.kind, "upgrade")
        self.assertEqual(new.start_date, date(2026, 3, 20))
        self.assertEqual(new.previous_id, old.pk)
        self.assertEqual((old.status, old.ended_on), ("superseded", date(2026, 3, 20)))
        self.assertEqual((old.start_date, old.end_date, old.price), (TODAY, date(2026, 4, 9), Decimal("5000")))
        self.assertEqual(self.member.memberships.count(), 2)

    def test_downgrade_same_plan_and_blocked_cases(self):
        with on(TODAY):
            old = services.assign_membership(self.member, self.annual, TODAY, self.staff)
            with self.assertRaisesMessage(MembershipError, "different plan"):
                services.change_plan(old, self.annual, self.staff)
            new = services.change_plan(old, self.monthly, self.staff)
            self.assertEqual(new.kind, "downgrade")
            with self.assertRaisesMessage(MembershipError, "currently active"):
                services.change_plan(old, self.annual, self.staff)  # already superseded

    def test_change_blocked_while_renewal_queued(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
            services.renew_membership(m, self.staff)
            with self.assertRaisesMessage(MembershipError, "scheduled renewal"):
                services.change_plan(m, self.annual, self.staff)


class SuspensionTests(Base):
    def setUp(self):
        super().setUp()
        with on(TODAY):
            self.m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)  # to Apr 9

    def test_suspend_and_reactivate_extends_end_date(self):
        with on(date(2026, 3, 15)):
            services.suspend_membership(self.m, self.staff, "Injury")
        self.m.refresh_from_db()
        self.assertEqual(self.m.status, "suspended")
        self.assertFalse(self.m.is_covering)
        with on(date(2026, 3, 25)):
            m, credited = services.reactivate_membership(self.m, self.staff)
        self.assertEqual(credited, 10)
        self.assertEqual((m.status, m.end_date, m.suspended_days, m.suspended_on), ("active", date(2026, 4, 19), 10, None))
        self.assertEqual(m.start_date, TODAY)

    def test_suspension_credit_is_capped(self):
        with on(date(2026, 3, 11)):
            services.suspend_membership(self.m, self.staff, "Travel")
        with on(date(2026, 3, 11) + timedelta(days=200)):
            m, credited = services.reactivate_membership(self.m, self.staff)
        self.assertEqual(credited, 90)

    @override_settings(MEMBERSHIP_MAX_SUSPENSION_DAYS=5)
    def test_cap_is_configurable(self):
        with on(date(2026, 3, 11)):
            services.suspend_membership(self.m, self.staff, "Travel")
        with on(date(2026, 3, 30)):
            _, credited = services.reactivate_membership(self.m, self.staff)
        self.assertEqual(credited, 5)

    def test_queued_renewal_shifts_with_reactivation(self):
        with on(date(2026, 3, 12)):
            renewal = services.renew_membership(self.m, self.staff)  # Apr 10 – May 9
            services.suspend_membership(self.m, self.staff, "Injury")
        with on(date(2026, 3, 22)):
            services.reactivate_membership(self.m, self.staff)
        renewal.refresh_from_db()
        self.m.refresh_from_db()
        self.assertEqual(self.m.end_date, date(2026, 4, 19))
        self.assertEqual((renewal.start_date, renewal.end_date), (date(2026, 4, 20), date(2026, 5, 19)))

    def test_invalid_transitions(self):
        with on(date(2026, 3, 15)):
            with self.assertRaisesMessage(MembershipError, "reason"):
                services.suspend_membership(self.m, self.staff, "  ")
            with self.assertRaisesMessage(MembershipError, "not suspended"):
                services.reactivate_membership(self.m, self.staff)
            services.suspend_membership(self.m, self.staff, "Injury")
            with self.assertRaisesMessage(MembershipError, "currently active"):
                services.suspend_membership(self.m, self.staff, "Again")
            with self.assertRaisesMessage(MembershipError, "Reactivate"):
                services.renew_membership(self.m, self.staff)


class CancellationTests(Base):
    def test_cancel_keeps_record_and_ends_access(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
            with self.assertRaisesMessage(MembershipError, "reason"):
                services.cancel_membership(m, self.staff, "")
            services.cancel_membership(m, self.staff, "Moving abroad")
        m.refresh_from_db()
        self.assertEqual((m.status, m.ended_on, m.end_date), ("cancelled", TODAY, date(2026, 4, 9)))
        self.assertFalse(m.is_covering)
        with on(TODAY):
            with self.assertRaisesMessage(MembershipError, "closed"):
                services.cancel_membership(m, self.staff, "again")
            # a cancelled member can buy a new membership immediately
            services.assign_membership(self.member, self.annual, TODAY, self.staff)

    def test_cannot_cancel_expired_or_renewed(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
            r = services.renew_membership(m, self.staff)
            with self.assertRaisesMessage(MembershipError, "scheduled renewal"):
                services.cancel_membership(m, self.staff, "x")
            services.cancel_membership(r, self.staff, "Changed mind")  # scheduled ones can be cancelled
        with on(date(2026, 6, 1)):
            with self.assertRaisesMessage(MembershipError, "expired"):
                services.cancel_membership(m, self.staff, "late")


class QueryTests(Base):
    def test_member_status_and_expiry_lists(self):
        soon, gone, fine, renewed = (make_member() for _ in range(4))
        with on(date(2026, 1, 1)):
            services.assign_membership(gone, self.monthly, date(2026, 1, 1), self.staff)  # ended Jan 31
        with on(date(2026, 2, 20)):
            a = services.assign_membership(soon, self.monthly, date(2026, 2, 20), self.staff)  # ends Mar 19
            b = services.assign_membership(renewed, self.monthly, date(2026, 2, 20), self.staff)
            services.renew_membership(b, self.staff)
        with on(TODAY):
            services.assign_membership(fine, self.annual, TODAY, self.staff)
            status = dict(members_with_status(Member.objects.all()).values_list("full_name", "membership_status"))
            self.assertEqual(status[gone.full_name], "expired")
            self.assertEqual(status[soon.full_name], "active")
            self.assertEqual(status[fine.full_name], "active")
            self.assertEqual(status[self.member.full_name], "none")
            expiring = {m.member_id for m in expiring_soon(14)}
            self.assertEqual(expiring, {soon.pk})  # renewed member excluded, annual not yet due
            self.assertEqual({m.member_id for m in expired_memberships()}, {gone.pk})
            self.assertEqual({m.pk for m in expiring_soon(14)}, {a.pk})

    def test_inactive_and_suspended_states(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
            services.suspend_membership(m, self.staff, "x")
            self.assertEqual(members_with_status(Member.objects.filter(pk=self.member.pk)).get().membership_status, "suspended")
            self.member.is_active = False
            self.member.save()
            self.assertEqual(members_with_status(Member.objects.filter(pk=self.member.pk)).get().membership_status, "inactive")

    def test_dashboard_statistics_use_real_data(self):
        a, b, c = make_member(), make_member(), make_member(registration_date=date(2026, 2, 1))
        with on(date(2026, 1, 1)):
            services.assign_membership(b, self.monthly, date(2026, 1, 1), self.staff)
        with on(date(2026, 3, 1)):
            services.assign_membership(a, self.monthly, date(2026, 3, 1), self.staff)  # ends Mar 31
        with on(TODAY):
            stats = get_dashboard_stats()
        s = stats["stats"]
        self.assertEqual(s["total_members"], 4)  # incl. self.member
        self.assertEqual((s["active_members"], s["expired_members"]), (1, 1))
        self.assertEqual(s["new_members"], 3)  # self.member plus a and b (registered "today"); c registered in February
        self.assertEqual(stats["upcoming_expirations"], [])


class PlanManagementTests(Base):
    def form(self, **kw):
        data = {"name": "Gold", "description": "", "duration_type": "quarterly", "custom_days": "", "price": "12000.00",
                "benefits": "Pool\nLocker", "access_restrictions": "", "is_active": "on"}
        data.update(kw)
        return data

    def test_manager_creates_custom_and_standard_plans(self):
        self.login("manager")
        r = self.client.post(reverse("memberships:plan_create"), self.form())
        p = MembershipPlan.objects.get(name="Gold")
        self.assertRedirects(r, p.get_absolute_url())
        self.assertEqual(p.benefit_list, ["Pool", "Locker"])
        r = self.client.post(reverse("memberships:plan_create"), self.form(name="45 days", duration_type="custom", custom_days="45"))
        self.assertEqual(MembershipPlan.objects.get(name="45 days").duration_label, "45 days")

    def test_plan_validation(self):
        self.login("manager")
        for bad in (self.form(duration_type="custom", custom_days=""), self.form(price="-5"), self.form(name="monthly"),
                    self.form(duration_type="custom", custom_days="99999")):
            self.assertEqual(self.client.post(reverse("memberships:plan_create"), bad).status_code, 200)
        self.assertEqual(MembershipPlan.objects.count(), 2)

    def test_standard_plan_drops_stray_custom_days(self):
        self.login("manager")
        self.client.post(reverse("memberships:plan_create"), self.form(custom_days="9"))
        self.assertIsNone(MembershipPlan.objects.get(name="Gold").custom_days)

    def test_only_price_permitted_staff_can_change_prices(self):
        RolePermission.objects.create(role="receptionist", permission="plans.manage")  # can edit, not price
        self.login("receptionist")
        self.assertEqual(self.client.get(reverse("memberships:plan_create")).status_code, 403)
        r = self.client.post(reverse("memberships:plan_edit", args=[self.monthly.pk]),
                             self.form(name="Monthly", duration_type="monthly", price="1.00", description="new text"))
        self.assertEqual(r.status_code, 302)
        self.monthly.refresh_from_db()
        self.assertEqual(self.monthly.price, Decimal("5000.00"))  # price ignored
        self.assertEqual(self.monthly.description, "new text")  # other fields still editable

        self.login("manager")
        self.client.post(reverse("memberships:plan_edit", args=[self.monthly.pk]),
                         self.form(name="Monthly", duration_type="monthly", price="6000"))
        self.monthly.refresh_from_db()
        self.assertEqual(self.monthly.price, Decimal("6000.00"))
        from apps.core.models import ActivityLog

        self.assertTrue(ActivityLog.objects.filter(action="plan_price_changed").exists())

    def test_plan_permissions(self):
        for role, view, manage in (("trainer", 200, 403), ("receptionist", 200, 403), ("manager", 200, 200), ("member", 403, 403)):
            self.login(role)
            self.assertEqual(self.client.get(reverse("memberships:plan_list")).status_code, view, role)
            self.assertEqual(self.client.get(reverse("memberships:plan_edit", args=[self.monthly.pk])).status_code, manage, role)
            self.assertEqual(self.client.post(reverse("memberships:plan_toggle_active", args=[self.monthly.pk])).status_code,
                             302 if manage == 200 else 403, role)

    def test_deactivating_plan_does_not_touch_existing_memberships(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
        self.login("manager")
        self.client.post(reverse("memberships:plan_toggle_active", args=[self.monthly.pk]))
        self.monthly.refresh_from_db()
        m.refresh_from_db()
        self.assertFalse(self.monthly.is_active)
        self.assertEqual(m.status, "active")
        self.assertNotIn(self.monthly, list(self.client.get(reverse("memberships:assign", args=[self.member.pk])).context["form"].fields["plan"].queryset))


class MembershipViewTests(Base):
    def test_assign_renew_suspend_cancel_flow_through_views(self):
        self.login("receptionist")
        today = timezone.localdate()
        r = self.client.post(reverse("memberships:assign", args=[self.member.pk]),
                             {"plan": self.monthly.pk, "start_date": today.isoformat(), "notes": ""})
        m = Membership.objects.get()
        self.assertRedirects(r, m.get_absolute_url())
        self.assertEqual(self.client.get(m.get_absolute_url()).status_code, 200)

        r = self.client.post(reverse("memberships:renew", args=[m.pk]), {"plan": self.monthly.pk, "notes": ""})
        self.assertEqual(Membership.objects.count(), 2)

        # a second renewal shows a rule error, not a crash
        r = self.client.post(reverse("memberships:renew", args=[m.pk]), {"plan": self.monthly.pk, "notes": ""})
        self.assertContains(r, "already been renewed")
        self.assertEqual(Membership.objects.count(), 2)

        # receptionists cannot cancel
        self.assertEqual(self.client.get(reverse("memberships:cancel", args=[m.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("memberships:cancel", args=[m.pk]), {"reason": "x" * 5}).status_code, 403)

        self.login("manager")
        renewal = Membership.objects.get(previous=m)
        r = self.client.post(reverse("memberships:cancel", args=[renewal.pk]), {"reason": "Changed my mind"})
        self.assertRedirects(r, renewal.get_absolute_url())
        renewal.refresh_from_db()
        self.assertEqual(renewal.status, "cancelled")

        self.client.post(reverse("memberships:suspend", args=[m.pk]), {"reason": "Injury"})
        m.refresh_from_db()
        self.assertEqual(m.status, "suspended")
        self.assertEqual(self.client.get(reverse("memberships:reactivate", args=[m.pk])).status_code, 405)
        self.client.post(reverse("memberships:reactivate", args=[m.pk]))
        m.refresh_from_db()
        self.assertEqual(m.status, "active")

    def test_assign_form_validation(self):
        self.login("receptionist")
        yesterday = timezone.localdate() - timedelta(days=1)
        r = self.client.post(reverse("memberships:assign", args=[self.member.pk]),
                             {"plan": self.monthly.pk, "start_date": yesterday.isoformat()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Membership.objects.count(), 0)

    def test_permissions_on_membership_pages(self):
        with on(TODAY):
            m = services.assign_membership(self.member, self.monthly, TODAY, self.staff)
        pages = [reverse("memberships:list"), reverse("memberships:expiring"), reverse("memberships:expired"),
                 m.get_absolute_url()]
        actions = [reverse("memberships:assign", args=[self.member.pk]), reverse("memberships:renew", args=[m.pk]),
                   reverse("memberships:change", args=[m.pk]), reverse("memberships:suspend", args=[m.pk])]
        for role in ("trainer", "accountant"):
            self.login(role)
            for url in pages:
                self.assertEqual(self.client.get(url).status_code, 200, (role, url))
            for url in actions:
                self.assertEqual(self.client.get(url).status_code, 403, (role, url))
            self.assertEqual(self.client.post(reverse("memberships:reactivate", args=[m.pk])).status_code, 403)
        self.login("member")
        for url in pages + actions:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.client.logout()
        for url in pages + actions:
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_list_filters_search_sort_pagination(self):
        with on(TODAY):
            services.assign_membership(self.member, self.monthly, TODAY, self.staff)
        for i in range(20):
            other = make_member(full_name=f"Bulk {i:02d}")
            Membership.objects.create(member=other, plan=self.annual, start_date=date(2020, 1, 1), end_date=date(2020, 12, 31), price=1)
        self.login("receptionist")
        r = self.client.get(reverse("memberships:list"))
        self.assertEqual(len(r.context["object_list"]), 15)
        r = self.client.get(reverse("memberships:list"), {"status": "expired"})
        self.assertEqual(r.context["page_obj"].paginator.count, 21)  # 20 bulk + the March membership
        r = self.client.get(reverse("memberships:list"), {"q": "Bulk 03"})
        self.assertEqual(r.context["page_obj"].paginator.count, 1)
        r = self.client.get(reverse("memberships:list"), {"plan": self.monthly.pk, "sort": "price", "dir": "desc"})
        self.assertEqual(r.context["page_obj"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("memberships:expired"), {"page": 2}).status_code, 200)

    def test_expiring_page_shows_only_unrenewed_members(self):
        today = timezone.localdate()
        soon = make_member(full_name="Soon Person")
        Membership.objects.create(member=soon, plan=self.monthly, start_date=today - timedelta(days=25),
                                  end_date=today + timedelta(days=5), price=1)
        self.login("receptionist")
        r = self.client.get(reverse("memberships:expiring"))
        self.assertContains(r, "Soon Person")
        r = self.client.get(reverse("memberships:expiring"), {"days": 3})
        self.assertEqual(r.context["days"], 14)  # unsupported windows fall back to the default

    def test_dashboard_shows_upcoming_expirations(self):
        today = timezone.localdate()
        soon = make_member(full_name="Soon Person")
        Membership.objects.create(member=soon, plan=self.monthly, start_date=today - timedelta(days=25),
                                  end_date=today + timedelta(days=5), price=1)
        self.login("manager")
        r = self.client.get(reverse("core:home"))
        self.assertContains(r, "Soon Person")
        self.assertEqual(r.context["stats"]["active_members"], 1)
        self.assertEqual(r.context["stats"]["total_members"], 2)


class PageRenderingSmokeTests(Base):
    """Every page renders (HTTP 200) for a manager with realistic data present."""

    def test_all_pages_render(self):
        today = timezone.localdate()
        m = services.assign_membership(self.member, self.monthly, today, self.staff)
        renewal = services.renew_membership(m, self.staff)
        suspended_member = make_member()
        s = services.assign_membership(suspended_member, self.annual, today, self.staff)
        services.suspend_membership(s, self.staff, "Injury")
        self.login("manager")
        urls = [
            reverse("core:home"), reverse("members:list"), reverse("members:create"),
            self.member.get_absolute_url(), reverse("members:edit", args=[self.member.pk]),
            suspended_member.get_absolute_url(),
            reverse("memberships:plan_list"), reverse("memberships:plan_create"),
            reverse("memberships:plan_detail", args=[self.monthly.pk]), reverse("memberships:plan_edit", args=[self.monthly.pk]),
            reverse("memberships:list"), reverse("memberships:expired"), reverse("memberships:expiring"),
            m.get_absolute_url(), renewal.get_absolute_url(), s.get_absolute_url(),
            reverse("memberships:assign", args=[make_member().pk]), reverse("memberships:renew", args=[m.pk]),
            reverse("memberships:change", args=[m.pk]), reverse("memberships:suspend", args=[m.pk]),
            reverse("memberships:cancel", args=[m.pk]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(s.get_absolute_url()), "Reactivate")
        self.assertContains(self.client.get(reverse("members:list")), "Suspended")

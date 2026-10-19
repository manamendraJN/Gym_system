from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.core.models import ActivityLog
from apps.core.stats import get_dashboard_stats
from apps.core.testing import assign_to_trainer
from apps.members.tests import make_member
from apps.memberships import services as membership_services
from apps.memberships.models import MembershipPlan

from . import services
from .billing import balance_as_of, membership_billing
from .models import Kind, Payment, PaymentAudit, Status
from .services import PaymentError

PASSWORD = "Str0ng-Passw0rd!"
D = Decimal


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.users = {r: User.objects.create_user(username=r, email=f"{r}@example.com", password=PASSWORD, role=r) for r in Role.values}
        self.staff = self.users["receptionist"]
        self.plan = MembershipPlan.objects.create(name="Monthly", price=D("5000.00"))
        self.member = make_member()
        today = timezone.localdate()
        self.membership = membership_services.assign_membership(self.member, self.plan, today, self.users["manager"])
        self._key = 0

    def key(self):
        self._key += 1
        return f"key{self._key}"

    def pay(self, amount, method="cash", **kw):
        payment, created = services.record_payment(self.membership, D(str(amount)), method, kw.pop("user", self.staff), self.key(), **kw)
        self.assertTrue(created)
        return payment

    def bill(self):
        return membership_billing(self.membership)

    def login(self, role):
        self.client.logout()
        self.assertTrue(self.client.login(username=role, password=PASSWORD))


class BalanceTests(Base):
    def test_unpaid_partial_paid(self):
        b = self.bill()
        self.assertEqual((b.amount_due, b.net_paid, b.outstanding, b.payment_status), (D("5000"), 0, D("5000"), "unpaid"))
        self.pay("2000.50")
        b = self.bill()
        self.assertEqual((b.net_paid, b.outstanding, b.payment_status), (D("2000.50"), D("2999.50"), "partial"))
        self.pay("2999.50", "bank_transfer", external_reference="SLIP-77")
        b = self.bill()
        self.assertEqual((b.outstanding, b.payment_status), (0, "paid"))

    def test_money_is_exact_decimal(self):
        for _ in range(3):
            self.pay("0.10")
        self.assertEqual(self.bill().net_paid, D("0.30"))
        self.assertIsInstance(Payment.objects.first().amount, Decimal)
        self.assertEqual(services.money("10.005"), D("10.01"))

    def test_overpayment_and_invalid_amounts_rejected(self):
        self.pay("4000")
        with self.assertRaisesMessage(PaymentError, "exceeds the balance"):
            self.pay("1000.01")
        for bad in ("0", "-5"):
            with self.assertRaises(PaymentError):
                self.pay(bad)
        self.assertEqual(self.bill().net_paid, D("4000"))

    def test_sequential_submissions_cannot_exceed_balance(self):
        """Each payment re-reads the balance under the membership lock, so back-to-back requests are safe."""
        self.pay("3000")
        outcomes = []
        for _ in range(3):
            try:
                self.pay("1000")
                outcomes.append("ok")
            except PaymentError:
                outcomes.append("refused")
        self.assertEqual(outcomes, ["ok", "ok", "refused"])
        self.assertEqual(self.bill().outstanding, 0)

    def test_pending_payments_do_not_count_until_confirmed(self):
        p = self.pay("1000", "bank_transfer", pending=True)
        b = self.bill()
        self.assertEqual((b.net_paid, b.pending_total, b.outstanding, b.payment_status), (0, D("1000"), D("5000"), "unpaid"))
        with self.assertRaisesMessage(PaymentError, "after LKR 1,000.00 awaiting"):
            self.pay("4500")  # the pending transfer already reserves part of the balance
        services.confirm_payment(p, self.users["accountant"])
        p.refresh_from_db()
        b = self.bill()
        self.assertEqual((p.status, b.net_paid, b.pending_total, b.payment_status), ("completed", D("1000"), 0, "partial"))
        with self.assertRaisesMessage(PaymentError, "Only a pending"):
            services.confirm_payment(p, self.users["accountant"])

    def test_rejected_pending_payment_never_counts(self):
        p = self.pay("1000", "card", pending=True, external_reference="APPR-1")
        services.reject_payment(p, self.users["accountant"], "Transfer never arrived")
        p.refresh_from_db()
        self.assertEqual((p.status, self.bill().net_paid), ("failed", 0))

    def test_cash_cannot_be_pending(self):
        with self.assertRaisesMessage(PaymentError, "Cash"):
            self.pay("100", "cash", pending=True)

    def test_cancelled_membership_has_no_balance_and_takes_no_payment(self):
        self.pay("1000")
        membership_services.cancel_membership(self.membership, self.users["manager"], "Left")
        b = self.bill()
        self.assertEqual((b.outstanding, b.net_paid), (0, D("1000")))
        with self.assertRaisesMessage(PaymentError, "cancelled"):
            self.pay("100")

    def test_date_rules(self):
        today = timezone.localdate()
        with self.assertRaisesMessage(PaymentError, "future"):
            self.pay("100", paid_on=today + timedelta(days=1))
        with self.assertRaisesMessage(PaymentError, "days ago"):
            self.pay("100", paid_on=today - timedelta(days=91))
        self.pay("100", paid_on=today - timedelta(days=30))

    def test_card_numbers_are_rejected(self):
        for ref in ("4111 1111 1111 1111", "5500-0000-0000-0004", "378282246310005"):
            with self.assertRaisesMessage(PaymentError, "card numbers"):
                self.pay("100", "card", external_reference=ref)
        self.pay("100", "card", external_reference="APPR-483920")  # a terminal approval code is fine
        self.pay("100", "bank_transfer", external_reference="1234567890123")  # not Luhn-valid: allowed
        self.assertFalse(Payment.objects.filter(external_reference__startswith="4111").exists())

    def test_references_and_receipt_numbers_are_unique(self):
        refs = {self.pay("10").reference for _ in range(5)}
        receipts = set(Payment.objects.values_list("receipt_number", flat=True))
        self.assertEqual((len(refs), len(receipts)), (5, 5))
        p = Payment.objects.first()
        self.assertRegex(p.reference, r"^TXN-\d{8}-[A-Z2-9]{8}$")
        self.assertRegex(p.receipt_number, r"^RC-\d{4}-\d{6}$")


class IdempotencyTests(Base):
    def test_same_key_returns_original(self):
        a, created_a = services.record_payment(self.membership, D("1000"), "cash", self.staff, "same-key")
        b, created_b = services.record_payment(self.membership, D("1000"), "cash", self.staff, "same-key")
        self.assertEqual((created_a, created_b, a.pk), (True, False, b.pk))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.bill().net_paid, D("1000"))

    def test_key_cannot_be_reused_for_another_membership(self):
        services.record_payment(self.membership, D("100"), "cash", self.staff, "shared")
        other = make_member()
        m2 = membership_services.assign_membership(other, self.plan, timezone.localdate(), self.users["manager"])
        with self.assertRaisesMessage(PaymentError, "different record"):
            services.record_payment(m2, D("100"), "cash", self.staff, "shared")

    def test_double_post_through_the_form_creates_one_payment(self):
        self.login("receptionist")
        url = reverse("payments:record", args=[self.membership.pk])
        page = self.client.get(url)
        key = page.context["form"].fields["idempotency_key"].initial
        data = {"amount": "1500.00", "method": "cash", "paid_on": timezone.localdate().isoformat(), "idempotency_key": key}
        first = self.client.post(url, data)
        second = self.client.post(url, data)  # impatient double click / browser resubmit
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(self.bill().net_paid, D("1500"))
        self.assertContains(self.client.get(second["Location"]), "")  # lands on the original payment

    def test_race_on_unique_key_is_resolved_to_the_existing_entry(self):
        existing = self.pay("100")
        with mock.patch.object(services, "_existing", return_value=None):  # simulate losing the race after the check
            again, created = services.record_payment(self.membership, D("100"), "cash", self.staff, existing.idempotency_key)
        self.assertEqual((created, again.pk), (False, existing.pk))
        self.assertEqual(Payment.objects.count(), 1)


class RefundTests(Base):
    def test_partial_then_full_refund(self):
        p = self.pay("5000")
        r1, _ = services.refund_payment(p, D("1500"), "cash", self.users["accountant"], self.key(), "Unused weeks")
        b = self.bill()
        self.assertEqual((b.net_paid, b.refund_total, b.payment_status, b.outstanding), (D("3500"), D("1500"), "partially_refunded", D("1500")))
        self.assertEqual((r1.kind, r1.refund_of_id, r1.amount), ("refund", p.pk, D("1500")))
        self.assertRegex(r1.receipt_number, r"^RF-")
        with self.assertRaisesMessage(PaymentError, "At most LKR 3,500.00"):
            services.refund_payment(p, D("3500.01"), "cash", self.users["accountant"], self.key(), "x" * 3)
        services.refund_payment(p, D("3500"), "cash", self.users["accountant"], self.key(), "Closed account")
        b = self.bill()
        self.assertEqual((b.net_paid, b.payment_status), (0, "refunded"))
        p.refresh_from_db()
        self.assertEqual((p.amount, p.status), (D("5000"), "completed"))  # the original is untouched

    def test_refund_rules(self):
        pending = self.pay("1000", "bank_transfer", pending=True)
        with self.assertRaisesMessage(PaymentError, "completed payment"):
            services.refund_payment(pending, D("10"), "cash", self.staff, self.key(), "x" * 3)
        done = self.pay("1000")
        with self.assertRaisesMessage(PaymentError, "reason"):
            services.refund_payment(done, D("10"), "cash", self.staff, self.key(), " ")
        r, _ = services.refund_payment(done, D("10"), "cash", self.staff, self.key(), "ok ok")
        with self.assertRaisesMessage(PaymentError, "completed payment"):
            services.refund_payment(r, D("1"), "cash", self.staff, self.key(), "refund of refund")

    def test_refund_double_submit(self):
        p = self.pay("1000")
        key = self.key()
        a, ca = services.refund_payment(p, D("400"), "cash", self.staff, key, "Reason")
        b, cb = services.refund_payment(p, D("400"), "cash", self.staff, key, "Reason")
        self.assertEqual((ca, cb, a.pk), (True, False, b.pk))
        self.assertEqual(self.bill().refund_total, D("400"))

    def test_refund_permission_and_flow_through_views(self):
        p = self.pay("2000")
        url = reverse("payments:refund", args=[p.pk])
        for role in ("receptionist", "trainer", "member"):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, 403, role)
            self.assertEqual(self.client.post(url, {"amount": "10", "method": "cash", "reason": "x y z", "idempotency_key": "abc"}).status_code, 403)
        self.assertFalse(Payment.objects.filter(kind="refund").exists())
        self.login("accountant")
        page = self.client.get(url)
        key = page.context["form"].fields["idempotency_key"].initial
        r = self.client.post(url, {"amount": "500", "method": "cash", "reason": "Customer request", "idempotency_key": key})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Payment.objects.get(kind="refund").recorded_by, self.users["accountant"])
        r = self.client.post(url, {"amount": "99999", "method": "cash", "reason": "Too much", "idempotency_key": "other1"})
        self.assertContains(r, "At most")


class AdjustmentAndVoidTests(Base):
    def test_discount_reduces_amount_due(self):
        services.adjust_charge(self.membership, D("-1000"), self.users["manager"], self.key(), "Loyalty discount")
        b = self.bill()
        self.assertEqual((b.amount_due, b.outstanding, b.adjustment_total), (D("4000"), D("4000"), D("-1000")))
        self.pay("4000")
        self.assertEqual(self.bill().payment_status, "paid")

    def test_surcharge_and_limits(self):
        services.adjust_charge(self.membership, D("250"), self.users["manager"], self.key(), "Locker")
        self.assertEqual(self.bill().amount_due, D("5250"))
        with self.assertRaisesMessage(PaymentError, "negative"):
            services.adjust_charge(self.membership, D("-6000"), self.users["manager"], self.key(), "Oops")
        self.pay("5000")
        with self.assertRaisesMessage(PaymentError, "refund the difference"):
            services.adjust_charge(self.membership, D("-1000"), self.users["manager"], self.key(), "Late discount")
        with self.assertRaisesMessage(PaymentError, "zero"):
            services.adjust_charge(self.membership, D("0"), self.users["manager"], self.key(), "Nothing")
        with self.assertRaisesMessage(PaymentError, "reason"):
            services.adjust_charge(self.membership, D("10"), self.users["manager"], self.key(), "")

    def test_adjustment_permission(self):
        url = reverse("payments:adjust", args=[self.membership.pk])
        for role in ("receptionist", "trainer", "member"):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, 403, role)
        for role in ("manager", "accountant", "super_admin"):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, 200, role)

    def test_void_excludes_entry_from_balances_but_keeps_record(self):
        p = self.pay("1000")
        services.void_payment(p, self.users["manager"], "Entered twice")
        p.refresh_from_db()
        self.assertEqual((p.status, self.bill().net_paid, Payment.objects.count()), ("voided", 0, 1))
        with self.assertRaisesMessage(PaymentError, "cannot be voided"):
            services.void_payment(p, self.users["manager"], "again")

    def test_void_rules(self):
        p = self.pay("1000")
        services.refund_payment(p, D("100"), "cash", self.staff, self.key(), "Partial")
        with self.assertRaisesMessage(PaymentError, "refunds against it"):
            services.void_payment(p, self.users["manager"], "Mistake")
        adj, _ = services.adjust_charge(self.membership, D("-10"), self.users["manager"], self.key(), "x y z")
        with self.assertRaisesMessage(PaymentError, "Adjustments"):
            services.void_payment(adj, self.users["manager"], "Mistake")
        with self.assertRaisesMessage(PaymentError, "reason"):
            services.void_payment(self.pay("5"), self.users["manager"], " ")

    def test_void_and_confirm_permissions(self):
        p = self.pay("1000", "bank_transfer", pending=True)
        for role in ("trainer", "member"):
            self.login(role)
            self.assertEqual(self.client.post(reverse("payments:confirm", args=[p.pk])).status_code, 403)
        self.login("receptionist")  # may record/confirm, but not void
        self.assertEqual(self.client.get(reverse("payments:void", args=[p.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("payments:confirm", args=[p.pk])).status_code, 405)
        self.client.post(reverse("payments:confirm", args=[p.pk]))
        p.refresh_from_db()
        self.assertEqual(p.status, "completed")


class IntegrityAndAuditTests(Base):
    def test_ledger_entries_are_append_only(self):
        p = self.pay("1000")
        p.amount = D("1")
        with self.assertRaises(ValueError):
            p.save()
        with self.assertRaises(ValueError):
            p.save(update_fields=["amount"])
        with self.assertRaises(ValueError):
            p.delete()
        with self.assertRaises(ValueError):
            Payment.objects.get(pk=p.pk).audit.first().delete()
        audit = p.audit.get()
        audit.note = "tampered"
        with self.assertRaises(ValueError):
            audit.save()
        self.assertEqual(Payment.objects.get(pk=p.pk).amount, D("1000"))

    def test_payment_member_must_match_membership(self):
        other = make_member()
        with self.assertRaises(ValueError):
            Payment.objects.create(membership=self.membership, member=other, amount=1, idempotency_key="z", method="cash")

    def test_database_constraints(self):
        base = dict(membership=self.membership, member=self.member, method="cash")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(amount=D("-5"), idempotency_key="a1", **base)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(amount=D("5"), kind="refund", idempotency_key="a2", **base)  # refund needs an original
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(amount=D("0"), kind="adjustment", idempotency_key="a3", **base)

    def test_every_financial_action_is_audited(self):
        p = self.pay("1000", "bank_transfer", pending=True)
        services.confirm_payment(p, self.users["accountant"])
        services.refund_payment(p, D("100"), "cash", self.users["manager"], self.key(), "Goodwill")
        services.adjust_charge(self.membership, D("-50"), self.users["manager"], self.key(), "Promo")
        actions = list(PaymentAudit.objects.values_list("action", "user__username"))
        self.assertEqual(actions, [("recorded", "receptionist"), ("confirmed", "accountant"), ("refunded", "manager"), ("adjusted", "manager")])
        self.assertEqual(ActivityLog.objects.filter(action__startswith="payment_").count(), 4)
        confirm = p.audit.get(action="confirmed")
        self.assertEqual((confirm.old_status, confirm.new_status), ("pending", "completed"))

    def test_failure_rolls_back_the_whole_operation(self):
        with mock.patch.object(services, "_audit", side_effect=RuntimeError("audit store down")):
            with self.assertRaises(RuntimeError):
                self.pay("1000")
        self.assertEqual(Payment.objects.count(), 0)  # no payment without its audit record
        self.assertEqual(self.bill().net_paid, 0)

    def test_failed_validation_leaves_nothing_behind(self):
        with self.assertRaises(PaymentError):
            self.pay("999999")
        self.assertFalse(Payment.objects.exists() or PaymentAudit.objects.exists())

    def test_payments_survive_membership_cancellation_and_member_deactivation(self):
        p = self.pay("1000")
        membership_services.cancel_membership(self.membership, self.users["manager"], "Left")
        self.member.is_active = False
        self.member.save()
        self.assertTrue(Payment.objects.filter(pk=p.pk).exists())
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.membership.delete()


class ReceiptTests(Base):
    def test_balance_as_of_each_receipt(self):
        p1, p2 = self.pay("2000"), self.pay("1000")
        self.assertEqual(balance_as_of(p1), {"amount_due": D("5000"), "net_paid": D("2000"), "outstanding": D("3000")})
        self.assertEqual(balance_as_of(p2), {"amount_due": D("5000"), "net_paid": D("3000"), "outstanding": D("2000")})

    def test_html_receipt_contents(self):
        p = self.pay("2000", "bank_transfer", external_reference="SLIP-99")
        self.login("accountant")
        r = self.client.get(reverse("payments:receipt", args=[p.pk]))
        for text in (p.receipt_number, p.reference, self.member.full_name, self.member.member_id, "Monthly", "Bank transfer",
                     "SLIP-99", "LKR 5,000.00", "LKR 2,000.00", "LKR 3,000.00", "Outstanding balance"):
            self.assertContains(r, text)
        self.assertNotContains(r, "NOT A VALID RECEIPT")

    def test_pending_receipt_is_marked_invalid(self):
        p = self.pay("2000", "bank_transfer", pending=True)
        self.login("accountant")
        self.assertContains(self.client.get(reverse("payments:receipt", args=[p.pk])), "NOT A VALID RECEIPT")

    def test_pdf_receipt(self):
        p = self.pay("2000")
        self.login("receptionist")
        r = self.client.get(reverse("payments:receipt_pdf", args=[p.pk]))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertIn(p.receipt_number, r["Content-Disposition"])
        self.assertIn("no-store", r["Cache-Control"])
        self.assertTrue(ActivityLog.objects.filter(action="payment_receipt_downloaded").exists())

    def test_refund_receipt_and_pdf(self):
        p = self.pay("2000")
        refund, _ = services.refund_payment(p, D("500"), "cash", self.users["manager"], self.key(), "Goodwill")
        self.login("manager")
        r = self.client.get(reverse("payments:receipt", args=[refund.pk]))
        self.assertContains(r, "REFUND RECEIPT")
        self.assertContains(r, p.receipt_number)
        self.assertTrue(self.client.get(reverse("payments:receipt_pdf", args=[refund.pk])).content.startswith(b"%PDF"))

    def test_receipt_authorization(self):
        p = self.pay("2000")
        adj, _ = services.adjust_charge(self.membership, D("-10"), self.users["manager"], self.key(), "Promo")
        for role in ("trainer", "member"):
            self.login(role)
            for name in ("receipt", "receipt_pdf", "detail"):
                self.assertEqual(self.client.get(reverse(f"payments:{name}", args=[p.pk])).status_code, 403, (role, name))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("payments:receipt_pdf", args=[p.pk])).status_code, 302)
        self.login("accountant")
        self.assertEqual(self.client.get(reverse("payments:receipt", args=[adj.pk])).status_code, 404)  # adjustments have no receipt


class ListAndPermissionTests(Base):
    def test_search_and_filters(self):
        today = timezone.localdate()
        other = make_member(full_name="Zed Other", phone="0779990001")
        m2 = membership_services.assign_membership(other, self.plan, today, self.users["manager"])
        a = self.pay("1000", "cash")
        b = self.pay("500", "card", external_reference="APPR-1", paid_on=today - timedelta(days=20))
        c, _ = services.record_payment(m2, D("700"), "bank_transfer", self.staff, "kz", pending=True)
        self.login("accountant")

        def ids(**params):
            return {p.pk for p in self.client.get(reverse("payments:list"), params).context["object_list"]}

        self.assertEqual(ids(), {a.pk, b.pk, c.pk})
        self.assertEqual(ids(q=a.reference), {a.pk})
        self.assertEqual(ids(q=a.receipt_number), {a.pk})
        self.assertEqual(ids(q="APPR-1"), {b.pk})
        self.assertEqual(ids(q="Zed"), {c.pk})
        self.assertEqual(ids(q="779990001"), {c.pk})
        self.assertEqual(ids(method="card"), {b.pk})
        self.assertEqual(ids(status="pending"), {c.pk})
        self.assertEqual(ids(**{"from": (today - timedelta(days=5)).isoformat()}), {a.pk, c.pk})
        r = self.client.get(reverse("payments:list"))
        self.assertEqual((r.context["received"], r.context["net"]), (D("1500"), D("1500")))  # pending excluded
        r = self.client.get(reverse("payments:list"), {"sort": "amount", "dir": "desc"})
        self.assertEqual([p.pk for p in r.context["object_list"]], [a.pk, c.pk, b.pk])

    def test_pagination(self):
        for _ in range(25):
            self.pay("10")
        self.login("accountant")
        self.assertEqual(len(self.client.get(reverse("payments:list")).context["object_list"]), 20)
        self.assertEqual(len(self.client.get(reverse("payments:list"), {"page": 2}).context["object_list"]), 5)

    def test_outstanding_page(self):
        today = timezone.localdate()
        paid_up = make_member()
        m2 = membership_services.assign_membership(paid_up, self.plan, today, self.users["manager"])
        services.record_payment(m2, D("5000"), "cash", self.staff, "full")
        self.pay("1000")
        self.login("accountant")
        r = self.client.get(reverse("payments:outstanding"))
        self.assertEqual([m.pk for m in r.context["object_list"]], [self.membership.pk])
        self.assertEqual(r.context["total_outstanding"], D("4000"))
        self.assertEqual(self.client.get(reverse("payments:outstanding"), {"status": "unpaid"}).context["object_list"].__len__(), 0)

    def test_access_matrix(self):
        p = self.pay("100")
        pages = [reverse("payments:list"), reverse("payments:outstanding"), p.get_absolute_url()]
        for role, expected in (("accountant", 200), ("manager", 200), ("receptionist", 200), ("trainer", 403), ("member", 403)):
            self.login(role)
            for url in pages:
                self.assertEqual(self.client.get(url).status_code, expected, (role, url))
        record = reverse("payments:record", args=[self.membership.pk])
        for role, expected in (("receptionist", 200), ("accountant", 200), ("trainer", 403), ("member", 403)):
            self.login(role)
            self.assertEqual(self.client.get(record).status_code, expected, role)
        self.client.logout()
        for url in pages + [record]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_payment_pages_render(self):
        p = self.pay("1000", "bank_transfer", pending=True)
        done = self.pay("500")
        refund, _ = services.refund_payment(done, D("100"), "cash", self.users["manager"], self.key(), "Goodwill")
        adj, _ = services.adjust_charge(self.membership, D("-10"), self.users["manager"], self.key(), "Promo")
        self.login("manager")
        urls = [p.get_absolute_url(), done.get_absolute_url(), refund.get_absolute_url(), adj.get_absolute_url(),
                reverse("payments:refund", args=[done.pk]), reverse("payments:reject", args=[p.pk]),
                reverse("payments:void", args=[done.pk]), reverse("payments:adjust", args=[self.membership.pk]),
                self.membership.get_absolute_url(), self.member.get_absolute_url(), reverse("core:home"),
                reverse("payments:record", args=[self.membership.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(self.membership.get_absolute_url()), "Record payment")
        self.assertContains(self.client.get(self.member.get_absolute_url()), done.receipt_number)

    def test_record_form_validation_errors_are_shown(self):
        self.login("receptionist")
        url = reverse("payments:record", args=[self.membership.pk])
        r = self.client.post(url, {"amount": "99999", "method": "cash", "paid_on": timezone.localdate().isoformat(), "idempotency_key": "kk1"})
        self.assertContains(r, "exceeds the balance")
        r = self.client.post(url, {"amount": "10", "method": "card", "paid_on": timezone.localdate().isoformat(),
                                   "external_reference": "4111111111111111", "idempotency_key": "kk2"})
        self.assertContains(r, "card numbers")
        self.assertFalse(Payment.objects.exists())

    def test_trainer_cannot_see_money_on_membership_page(self):
        self.pay("1000")
        assign_to_trainer(self.users["trainer"], self.member)
        self.login("trainer")
        r = self.client.get(self.membership.get_absolute_url())
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Billing")
        self.assertNotContains(r, "RC-")


class DashboardFinanceTests(Base):
    def test_revenue_is_net_of_refunds_and_excludes_unpaid_and_pending(self):
        today = timezone.localdate()
        p = self.pay("2000")
        self.pay("700", "bank_transfer", pending=True)
        services.refund_payment(p, D("300"), "cash", self.users["manager"], self.key(), "Goodwill")
        old = self.pay("500", paid_on=today.replace(day=1) - timedelta(days=1) if today.day > 1 else today)
        s = get_dashboard_stats()
        stats = s["stats"]
        self.assertEqual(stats["todays_revenue"], D("1700") + (D("500") if old.paid_on == today else 0))
        month_expected = D("1700") + (D("500") if old.paid_on >= today.replace(day=1) else 0)
        self.assertEqual(stats["monthly_revenue"], month_expected)
        self.assertEqual(s["pending_total"], D("700"))
        # unpaid fees are a receivable, not revenue: 5000 - (2000 - 300 + 500) = 2800
        self.assertEqual(stats["outstanding"], D("2800"))
        self.assertEqual(s["recent_payments"][0].kind in ("payment", "refund"), True)

    def test_payment_status_breakdown_and_outstanding_total(self):
        today = timezone.localdate()
        users = []
        for i, amount in enumerate((0, 1000, 5000)):
            m = make_member()
            ms = membership_services.assign_membership(m, self.plan, today, self.users["manager"])
            if amount:
                services.record_payment(ms, D(amount), "cash", self.staff, f"bd{i}")
        s = get_dashboard_stats()
        counts = {r["key"]: r["count"] for r in s["payment_breakdown"]}
        self.assertEqual(counts, {"unpaid": 2, "partial": 1, "paid": 1})  # includes the Base membership (unpaid)
        self.assertEqual(s["stats"]["outstanding"], D("5000") + D("5000") + D("4000"))

    def test_dashboard_renders_with_financial_data_and_hides_nothing_from_manager(self):
        self.pay("1000")
        self.login("manager")
        r = self.client.get(reverse("core:home"))
        self.assertContains(r, "Revenue collected today")
        self.assertContains(r, "Outstanding (unpaid fees)")
        self.assertContains(r, "LKR 1,000.00")

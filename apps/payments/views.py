from decimal import Decimal

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.listing import resolve_sort, sort_columns
from apps.core.models import ActivityLog
from apps.members.models import phone_search_term
from apps.memberships.forms import ReasonForm
from apps.appointments.models import Appointment
from apps.memberships.models import Membership

from . import forms, receipts, services
from .billing import PAYMENT_STATUS_LABELS, billing_for, membership_billing, with_billing
from .models import Kind, Method, Payment, Status
from .services import PaymentError


def _member_q(prefix, q):
    cond = Q(**{f"{prefix}full_name__icontains": q}) | Q(**{f"{prefix}member_id__icontains": q})
    digits = phone_search_term(q)
    if digits:
        cond |= Q(**{f"{prefix}phone__contains": digits})
    return cond


def _item_label(target):
    if isinstance(target, Appointment):
        local = timezone.localtime(target.start)
        return f"Personal training with {target.trainer.full_name} · {local:%b %d, %Y %H:%M} · {target.duration_minutes} min"
    return f"{target.plan.name} · {target.start_date:%b %d, %Y} – {target.end_date:%b %d, %Y}"


# ------------------------------------------------------------------------------------------- lists
class PaymentListView(GymPermissionMixin, ListView):
    required_permission = "payments.view"
    template_name = "payments/payment_list.html"
    paginate_by = 20
    SORTS = {"date": "paid_on", "amount": "amount", "member": "member__full_name"}
    COLUMNS = [("date", "Date"), ("member", "Member"), ("amount", "Amount")]

    def get_queryset(self):
        g = self.request.GET
        qs = Payment.objects.select_related("member", "membership__plan", "appointment__trainer", "recorded_by")
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(
                _member_q("member__", q) | Q(reference__icontains=q) | Q(receipt_number__icontains=q)
                | Q(external_reference__icontains=q)
            )
        for field, choices in (("kind", Kind.values), ("method", Method.values), ("status", Status.values)):
            if g.get(field) in choices:
                qs = qs.filter(**{field: g[field]})
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(paid_on__gte=d_from)
        if d_to:
            qs = qs.filter(paid_on__lte=d_to)
        self.filtered_qs = qs
        self.sort, self.direction, order = resolve_sort(self.request, self.SORTS, "date")
        if "sort" not in g:
            self.direction, order = "desc", ["-paid_on", "-pk"]
        return qs.order_by(*order)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        done = self.filtered_qs.filter(status=Status.COMPLETED)

        def total(kind):
            return done.filter(kind=kind).aggregate(t=Sum("amount"))["t"] or Decimal("0")

        received, refunded = total(Kind.PAYMENT), total(Kind.REFUND)
        ctx.update(
            q=g.get("q", ""), date_from=g.get("from", ""), date_to=g.get("to", ""),
            kind=g.get("kind", ""), method=g.get("method", ""), status=g.get("status", ""),
            kinds=Kind.choices, methods=Method.choices[:3], statuses=Status.choices,
            received=received, refunded=refunded, net=received - refunded,
            columns={c["key"]: c for c in sort_columns(self.request, self.COLUMNS, self.sort, self.direction)},
            filtered_on=any(g.get(k) for k in ("q", "from", "to", "kind", "method", "status")),
        )
        return ctx


class OutstandingView(GymPermissionMixin, ListView):
    """Unpaid membership fees. This is money owed, not revenue."""

    required_permission = "payments.view"
    template_name = "payments/outstanding.html"
    paginate_by = 20
    SORTS = {"outstanding": "outstanding", "member": "member__full_name", "end": "end_date"}
    COLUMNS = [("member", "Member"), ("end", "Period ends"), ("outstanding", "Outstanding")]

    def get_queryset(self):
        g = self.request.GET
        qs = with_billing(Membership.objects.select_related("member", "plan").filter(member__is_active=True)).filter(outstanding__gt=0)
        if g.get("q", "").strip():
            qs = qs.filter(_member_q("member__", g["q"].strip()))
        if g.get("status") in ("unpaid", "partial"):
            qs = qs.filter(payment_status=g["status"])
        self.filtered_qs = qs
        self.sort, self.direction, order = resolve_sort(self.request, self.SORTS, "outstanding")
        if "sort" not in g:
            self.direction, order = "desc", ["-outstanding", "pk"]
        return qs.order_by(*order)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        unpaid_sessions = with_billing(
            Appointment.objects.select_related("member", "trainer").exclude(status="cancelled").filter(member__is_active=True), "fee"
        ).filter(outstanding__gt=0).order_by("start")
        if g.get("q", "").strip():
            unpaid_sessions = unpaid_sessions.filter(_member_q("member__", g["q"].strip()))
        ctx.update(
            unpaid_sessions=unpaid_sessions[:50],
            q=g.get("q", ""), status=g.get("status", ""),
            total_outstanding=sum((m.outstanding for m in self.filtered_qs), Decimal("0")) + sum((a.outstanding for a in unpaid_sessions), Decimal("0")),
            columns={c["key"]: c for c in sort_columns(self.request, self.COLUMNS, self.sort, self.direction)},
        )
        return ctx


class PaymentDetailView(GymPermissionMixin, DetailView):
    required_permission = "payments.view"
    template_name = "payments/payment_detail.html"

    def get_queryset(self):
        return Payment.objects.select_related("member", "membership__plan", "appointment__trainer", "recorded_by", "refund_of")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        p, user = self.object, self.request.user
        done = p.status == Status.COMPLETED
        refunded = sum((r.amount for r in p.refunds.filter(status=Status.COMPLETED)), Decimal("0"))
        ctx.update(
            billing=billing_for(p.target), audit=p.audit.select_related("user"),
            refunds=p.refunds.all(), refundable=(p.amount - refunded) if p.kind == Kind.PAYMENT and done else Decimal("0"),
            can_confirm=p.status == Status.PENDING and user.has_gym_perm("payments.manage"),
            can_refund=p.kind == Kind.PAYMENT and done and user.has_gym_perm("payments.refund") and p.amount > refunded,
            can_void=p.kind != Kind.ADJUSTMENT and p.status in (Status.COMPLETED, Status.PENDING) and user.has_gym_perm("payments.adjust")
            and not (p.kind == Kind.PAYMENT and refunded),
            has_receipt=p.kind != Kind.ADJUSTMENT,
        )
        return ctx


# --------------------------------------------------------------------------------------- actions
class _MembershipActionView(GymPermissionMixin, FormView):
    template_name = "payments/action_form.html"
    title, submit_label, danger, intro = "", "Save", False, ""

    target_model = Membership

    @cached_property
    def membership(self):
        """The item being paid for: a Membership, or an Appointment on the appointment URLs."""
        if self.target_model is Appointment:
            return get_object_or_404(Appointment.objects.select_related("member", "trainer"), pk=self.kwargs["pk"])
        return get_object_or_404(Membership.objects.select_related("member", "plan"), pk=self.kwargs["pk"])

    @cached_property
    def billing(self):
        return billing_for(self.membership)

    def perform(self, form):
        raise NotImplementedError

    def form_valid(self, form):
        try:
            message, target = self.perform(form)
        except PaymentError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, message)
        return redirect(target)

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            title=self.title, submit_label=self.submit_label, danger=self.danger, intro=self.intro,
            membership=self.membership, member=self.membership.member, billing=self.billing,
            item_label=_item_label(self.membership), item_url=self.membership.get_absolute_url(), **kwargs,
        )


class RecordPaymentView(_MembershipActionView):
    required_permission = "payments.manage"
    form_class = forms.PaymentForm
    title, submit_label = "Record payment", "Record payment"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "outstanding": max(self.billing.outstanding - self.billing.pending_total, Decimal("0"))}

    def perform(self, form):
        c = form.cleaned_data
        payment, created = services.record_payment(
            self.membership, c["amount"], c["method"], self.request.user, c["idempotency_key"], paid_on=c["paid_on"],
            external_reference=c["external_reference"], notes=c["notes"], pending=c["pending"],
        )
        if not created:
            return f"This payment was already recorded as {payment.receipt_number}; nothing was added twice.", payment
        word = "recorded as pending" if payment.status == Status.PENDING else "recorded"
        return f"Payment of LKR {payment.amount:,.2f} {word} ({payment.receipt_number}).", payment


class RecordAppointmentPaymentView(RecordPaymentView):
    target_model = Appointment


class AdjustChargeView(_MembershipActionView):
    required_permission = "payments.adjust"
    form_class = forms.AdjustmentForm
    title, submit_label, danger = "Adjust amount due", "Apply adjustment", True
    intro = "Adjustments change what the member owes and are permanently recorded with your name and the reason."

    def perform(self, form):
        c = form.cleaned_data
        adj, created = services.adjust_charge(self.membership, c["amount"], self.request.user, c["idempotency_key"], c["reason"])
        return ("Adjustment recorded." if created else "This adjustment was already recorded."), adj


class AdjustAppointmentChargeView(AdjustChargeView):
    target_model = Appointment


class _PaymentActionView(GymPermissionMixin, FormView):
    """Actions on one existing ledger entry (refund / reject / void)."""

    template_name = "payments/action_form.html"
    title, submit_label, danger, intro = "", "Save", False, ""

    @cached_property
    def payment(self):
        return get_object_or_404(Payment.objects.select_related("member", "membership__plan", "appointment__trainer"), pk=self.kwargs["pk"])

    def perform(self, form):
        raise NotImplementedError

    def form_valid(self, form):
        try:
            message, target = self.perform(form)
        except PaymentError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        messages.success(self.request, message)
        return redirect(target)

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            title=self.title, submit_label=self.submit_label, danger=self.danger, intro=self.intro,
            membership=self.payment.target, member=self.payment.member, payment=self.payment,
            item_label=_item_label(self.payment.target), item_url=self.payment.target.get_absolute_url(), **kwargs,
        )


class RefundView(_PaymentActionView):
    required_permission = "payments.refund"
    form_class = forms.RefundForm
    title, submit_label, danger = "Refund payment", "Issue refund", True
    intro = "A refund is a new, permanent entry linked to the original payment. The original is never changed."

    def get_form_kwargs(self):
        done = sum((r.amount for r in self.payment.refunds.filter(status=Status.COMPLETED)), Decimal("0"))
        return {**super().get_form_kwargs(), "refundable": max(self.payment.amount - done, Decimal("0")),
                "original_method": self.payment.method}

    def perform(self, form):
        c = form.cleaned_data
        refund, created = services.refund_payment(
            self.payment, c["amount"], c["method"], self.request.user, c["idempotency_key"], c["reason"], c["external_reference"])
        return (f"Refund of LKR {refund.amount:,.2f} recorded ({refund.receipt_number})." if created
                else "This refund was already recorded."), refund


class RejectPaymentView(_PaymentActionView):
    required_permission = "payments.manage"
    form_class = ReasonForm
    title, submit_label, danger = "Reject pending payment", "Reject payment", True
    intro = "Use this when the transfer or card payment never arrived. The entry is kept as 'Failed'."

    def perform(self, form):
        p = services.reject_payment(self.payment, self.request.user, form.cleaned_data["reason"])
        return "The pending payment was marked as failed.", p


class VoidPaymentView(_PaymentActionView):
    required_permission = "payments.adjust"
    form_class = ReasonForm
    title, submit_label, danger = "Void entry", "Void entry", True
    intro = "Voiding is for entries made by mistake. The entry stays on record but no longer counts towards balances."

    def perform(self, form):
        p = services.void_payment(self.payment, self.request.user, form.cleaned_data["reason"])
        return "The entry was voided.", p


class ConfirmPaymentView(GymPermissionMixin, View):
    """Staff confirmation of a pending payment. Only a signed-in, authorised user can do this; nothing is ever
    confirmed from a browser redirect."""

    required_permission = "payments.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        payment = get_object_or_404(Payment, pk=pk)
        try:
            services.confirm_payment(payment, request.user)
            messages.success(request, f"Payment {payment.receipt_number} confirmed.")
        except PaymentError as exc:
            messages.error(request, str(exc))
        return redirect(payment)


# ---------------------------------------------------------------------------------------- receipts
class ReceiptView(GymPermissionMixin, DetailView):
    required_permission = "payments.view"
    template_name = "payments/receipt.html"

    def get_queryset(self):
        return Payment.objects.exclude(kind=Kind.ADJUSTMENT).select_related("member", "membership__plan", "appointment__trainer", "recorded_by", "refund_of")

    def get_context_data(self, **kwargs):
        return super().get_context_data(**receipts.receipt_data(self.object), **kwargs)


class ReceiptPDFView(GymPermissionMixin, View):
    required_permission = "payments.view"

    def get(self, request, pk):
        payment = get_object_or_404(
            Payment.objects.exclude(kind=Kind.ADJUSTMENT).select_related("member", "membership__plan", "appointment__trainer", "recorded_by", "refund_of"), pk=pk)
        ActivityLog.record(request.user, "payment_receipt_downloaded", f"Downloaded receipt {payment.receipt_number}")
        response = HttpResponse(receipts.render_pdf(payment), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{payment.receipt_number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response

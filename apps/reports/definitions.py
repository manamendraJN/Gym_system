"""The business reports. Every figure is queried from the database at request time."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.attendance.models import Attendance
from apps.equipment.models import Equipment, EquipmentStatus, MaintenanceRecord
from apps.expenses.models import Expense
from apps.inventory.models import InventoryItem, MovementKind, StockMovement
from apps.members.models import Member, normalize_phone
from apps.memberships.models import STATUS_LABELS, Membership, MembershipPlan
from apps.memberships.queries import MEMBER_STATUS_LABELS, members_with_status
from apps.payments.billing import PAYMENT_STATUS_LABELS, with_billing
from apps.payments.models import Kind, Method, Payment, Status

from . import finance
from .base import Col, Report

ZERO = Decimal("0.00")


def day_range(report):
    """[from, to] bounds as aware datetimes for DateTime fields (to is inclusive of the whole last day)."""
    tz = timezone.get_current_timezone()
    from datetime import datetime, time

    start = timezone.make_aware(datetime.combine(report.date_from, time.min), tz)
    end = timezone.make_aware(datetime.combine(report.date_to + timedelta(days=1), time.min), tz)
    return start, end


class MemberRegistrations(Report):
    key, title, icon = "member-registrations", "Member registrations", "person-plus"
    description = "Members who registered in the period, with their current membership status."
    module_permission = "members.view"
    columns = (Col("member_id", "Member ID"), Col("name", "Name"), Col("phone", "Phone"), Col("email", "Email"),
               Col("registered", "Registered", "date"), Col("status", "Status"))

    def _members(self):
        return members_with_status(Member.objects.filter(registration_date__gte=self.date_from, registration_date__lte=self.date_to)).order_by("registration_date", "pk")

    def rows(self):
        return [{"member_id": m.member_id, "name": m.full_name, "phone": m.phone, "email": m.email, "registered": m.registration_date,
                 "status": MEMBER_STATUS_LABELS[m.membership_status]} for m in self._members()]

    def summary(self):
        rows = self._members()
        return [("Members registered", rows.count(), "int"), ("Still active", sum(1 for m in rows if m.membership_status == "active"), "int")]

    def chart(self):
        by_month = {}
        for m in self._members():
            by_month[m.registration_date.strftime("%b %Y")] = by_month.get(m.registration_date.strftime("%b %Y"), 0) + 1
        return {"type": "bar", "labels": list(by_month), "datasets": [{"label": "New members", "data": list(by_month.values())}]} if by_month else None


class MembershipStatusReport(Report):
    key, title, icon = "membership-status", "Active and expired memberships", "card-checklist"
    description = "Every membership as of today. Filter by status."
    module_permission = "memberships.view"
    uses_dates = False
    status_options = tuple((k, STATUS_LABELS[k]) for k in ("active", "scheduled", "expired", "suspended", "cancelled", "superseded"))
    columns = (Col("member_id", "Member ID"), Col("name", "Member"), Col("plan", "Plan"), Col("start", "Start", "date"), Col("end", "End", "date"),
               Col("status", "Status"), Col("days_left", "Days left", "int"), Col("price", "Price (LKR)", "money"))

    def _all(self):
        return list(Membership.objects.select_related("member", "plan").order_by("end_date", "pk"))

    def rows(self):
        today = timezone.localdate()
        out = []
        for m in self._all():
            key = m.display_status(today)
            if self.status and key != self.status:
                continue
            out.append({"member_id": m.member.member_id, "name": m.member.full_name, "plan": m.plan.name, "start": m.start_date, "end": m.end_date,
                        "status": STATUS_LABELS[key], "days_left": (m.end_date - today).days if key in ("active", "scheduled") else None, "price": m.price})
        return out

    def summary(self):
        today = timezone.localdate()
        counts = {}
        for m in self._all():
            counts[m.display_status(today)] = counts.get(m.display_status(today), 0) + 1
        return [(STATUS_LABELS[k], counts.get(k, 0), "int") for k in ("active", "scheduled", "expired", "suspended", "cancelled", "superseded")]

    def chart(self):
        s = self.summary()
        return {"type": "doughnut", "labels": [l for l, v, _ in s if v], "datasets": [{"label": "Memberships", "data": [v for _, v, _ in s if v]}]}


class RenewalReport(Report):
    key, title, icon = "renewals", "Membership renewals", "arrow-repeat"
    description = "Renewals that start in the period."
    module_permission = "memberships.view"
    columns = (Col("member_id", "Member ID"), Col("name", "Member"), Col("plan", "Plan"), Col("previous_end", "Previous period ended", "date"),
               Col("start", "Renewal starts", "date"), Col("end", "Renewal ends", "date"), Col("price", "Price (LKR)", "money"), Col("status", "Status"))

    def _qs(self):
        return Membership.objects.filter(kind="renewal", start_date__gte=self.date_from, start_date__lte=self.date_to).select_related("member", "plan", "previous").order_by("start_date", "pk")

    def rows(self):
        today = timezone.localdate()
        return [{"member_id": m.member.member_id, "name": m.member.full_name, "plan": m.plan.name, "previous_end": m.previous.end_date if m.previous else None,
                 "start": m.start_date, "end": m.end_date, "price": m.price, "status": STATUS_LABELS[m.display_status(today)]} for m in self._qs()]

    def summary(self):
        qs = self._qs().exclude(status="cancelled")
        return [("Renewals", self._qs().count(), "int"), ("Value of renewals (LKR)", qs.aggregate(t=Sum("price"))["t"] or ZERO, "money")]


class PlanPopularity(Report):
    key, title, icon = "plan-popularity", "Membership plan popularity", "tags"
    description = "How many memberships of each plan started in the period."
    module_permission = "memberships.view"
    columns = (Col("plan", "Plan"), Col("started", "Started in period", "int"), Col("renewals", "…of which renewals", "int"),
               Col("active_now", "Active now", "int"), Col("invoiced", "Invoiced (LKR)", "money"), Col("share", "Share of starts", "percent"))

    def rows(self):
        today = timezone.localdate()
        period = Q(memberships__start_date__gte=self.date_from, memberships__start_date__lte=self.date_to) & ~Q(memberships__status="cancelled")
        plans = MembershipPlan.objects.annotate(
            started=Count("memberships", filter=period), renewals=Count("memberships", filter=period & Q(memberships__kind="renewal")),
            active_now=Count("memberships", filter=Q(memberships__status="active", memberships__start_date__lte=today, memberships__end_date__gte=today)),
            invoiced=Sum("memberships__price", filter=period)).order_by("-started", "name")
        total = sum(p.started for p in plans) or 0
        return [{"plan": p.name, "started": p.started, "renewals": p.renewals, "active_now": p.active_now, "invoiced": p.invoiced or ZERO,
                 "share": round(p.started * 100 / total, 1) if total else 0} for p in plans]

    def chart(self):
        rows = [r for r in self.rows() if r["started"]]
        return {"type": "bar", "labels": [r["plan"] for r in rows], "datasets": [{"label": "Memberships started", "data": [r["started"] for r in rows]}]} if rows else None


class AttendanceReport(Report):
    key, title, icon = "attendance", "Daily and monthly attendance", "calendar-check"
    description = "Check-ins per day or per month."
    module_permission = "attendance.view"
    group_options = (("day", "Daily"), ("month", "Monthly"))
    columns = (Col("period", "Period"), Col("visits", "Check-ins", "int"), Col("members", "Unique members", "int"), Col("qr", "Via QR code", "int"))

    def rows(self):
        start, end = day_range(self)
        trunc = TruncMonth("check_in") if self.group == "month" else TruncDate("check_in")
        data = (Attendance.objects.filter(check_in__gte=start, check_in__lt=end).annotate(p=trunc).values("p")
                .annotate(visits=Count("pk"), members=Count("member", distinct=True), qr=Count("pk", filter=Q(method="qr"))).order_by("p"))
        return [{"period": r["p"].strftime("%b %Y") if self.group == "month" else r["p"], "visits": r["visits"], "members": r["members"], "qr": r["qr"]} for r in data]

    def summary(self):
        start, end = day_range(self)
        qs = Attendance.objects.filter(check_in__gte=start, check_in__lt=end)
        return [("Total check-ins", qs.count(), "int"), ("Unique members", qs.values("member").distinct().count(), "int")]

    def chart(self):
        rows = self.rows()
        return {"type": "bar", "labels": [str(r["period"]) for r in rows], "datasets": [{"label": "Check-ins", "data": [r["visits"] for r in rows]}]} if rows else None


class MemberAttendance(Report):
    key, title, icon = "member-attendance", "Member attendance history", "person-lines-fill"
    description = "Every visit of one member. Enter the member ID or phone number."
    module_permission = "attendance.view"
    needs_member = True
    columns = (Col("checkin", "Check-in", "datetime"), Col("checkout", "Check-out", "datetime"), Col("minutes", "Minutes", "int"), Col("method", "Method"), Col("plan", "Plan"))

    def member(self):
        value = self.member_query
        if not value:
            return None
        found = Member.objects.filter(member_id__iexact=value).first()
        if found is None and normalize_phone(value).lstrip("+"):
            found = Member.objects.filter(phone=normalize_phone(value)).first()
        return found

    def rows(self):
        member = self.member()
        if member is None:
            return []
        start, end = day_range(self)
        out = []
        for v in Attendance.objects.filter(member=member, check_in__gte=start, check_in__lt=end).select_related("membership__plan").order_by("check_in"):
            out.append({"checkin": v.check_in, "checkout": v.check_out, "minutes": int(v.duration.total_seconds() // 60) if v.check_out else None,
                        "method": v.get_method_display(), "plan": v.membership.plan.name if v.membership else ""})
        return out

    def summary(self):
        m = self.member()
        if m is None:
            return [("Member", "Enter a member ID or phone number above" if not self.member_query else "No member found", "text")]
        return [("Member", f"{m.full_name} ({m.member_id})", "text"), ("Visits in period", len(self.rows()), "int")]


class PaymentReport(Report):
    key, title, icon = "payments", "Daily and monthly payments", "cash-stack"
    description = "Completed payments and refunds by payment date."
    module_permission = "payments.view"
    group_options = (("day", "Daily"), ("month", "Monthly"))
    columns = (Col("period", "Period"), Col("count", "Payments", "int"), Col("gross", "Received (LKR)", "money"), Col("refunds", "Refunded (LKR)", "money"), Col("net", "Net collected (LKR)", "money"))

    def rows(self):
        qs = finance.ledger(self.date_from, self.date_to)
        key = TruncMonth("paid_on") if self.group == "month" else F("paid_on")
        data = (qs.annotate(p=key).values("p").annotate(
            count=Count("pk", filter=Q(kind=Kind.PAYMENT)), gross=Sum("amount", filter=Q(kind=Kind.PAYMENT)), refunds=Sum("amount", filter=Q(kind=Kind.REFUND))).order_by("p"))
        out = []
        for r in data:
            gross, refunds = r["gross"] or ZERO, r["refunds"] or ZERO
            out.append({"period": r["p"].strftime("%b %Y") if self.group == "month" else r["p"], "count": r["count"], "gross": gross, "refunds": refunds, "net": gross - refunds})
        return out

    def summary(self):
        t = finance.totals(self.date_from, self.date_to)
        pending = Payment.objects.filter(kind=Kind.PAYMENT, status=Status.PENDING, paid_on__gte=self.date_from, paid_on__lte=self.date_to)
        return [("Received", t["gross"], "money"), ("Refunded", t["refunds"], "money"), ("Net collected", t["net"], "money"),
                ("Awaiting confirmation (not counted)", pending.aggregate(t=Sum("amount"))["t"] or ZERO, "money")]

    def chart(self):
        rows = self.rows()
        return {"type": "bar", "labels": [str(r["period"]) for r in rows], "datasets": [{"label": "Net collected (LKR)", "data": [float(r["net"]) for r in rows]}]} if rows else None


class OutstandingReport(Report):
    key, title, icon = "outstanding", "Outstanding balances", "receipt"
    description = "Unpaid membership and training fees as of now (cancelled items are written off)."
    module_permission = "payments.view"
    uses_dates = False
    columns = (Col("type", "Type"), Col("member_id", "Member ID"), Col("name", "Member"), Col("phone", "Phone"), Col("item", "Item"), Col("due", "Amount due (LKR)", "money"),
               Col("paid", "Paid (LKR)", "money"), Col("outstanding", "Outstanding (LKR)", "money"), Col("status", "Payment status"))

    def rows(self):
        out = []
        for m in with_billing(Membership.objects.exclude(status="cancelled").select_related("member", "plan")).filter(outstanding__gt=0).order_by("-outstanding"):
            out.append({"type": "Membership", "member_id": m.member.member_id, "name": m.member.full_name, "phone": m.member.phone, "item": f"{m.plan.name} ({m.start_date:%b %d} – {m.end_date:%b %d, %Y})",
                        "due": m.amount_due, "paid": m.net_paid, "outstanding": m.outstanding, "status": PAYMENT_STATUS_LABELS[m.payment_status]})
        for a in with_billing(Appointment.objects.exclude(status="cancelled").select_related("member", "trainer"), "fee").filter(outstanding__gt=0).order_by("-outstanding"):
            out.append({"type": "Training session", "member_id": a.member.member_id, "name": a.member.full_name, "phone": a.member.phone,
                        "item": f"{a.trainer.full_name}, {timezone.localtime(a.start):%b %d, %Y %H:%M}", "due": a.amount_due, "paid": a.net_paid,
                        "outstanding": a.outstanding, "status": PAYMENT_STATUS_LABELS[a.payment_status]})
        return sorted(out, key=lambda r: r["outstanding"], reverse=True)

    def summary(self):
        rows = self.rows()
        return [("Items with a balance", len(rows), "int"), ("Total outstanding (not revenue)", sum((r["outstanding"] for r in rows), ZERO), "money")]


class RevenueReport(Report):
    key, title, icon = "revenue", "Revenue collected", "wallet2"
    description = "Invoiced, collected, refunded and outstanding amounts for the period, and revenue by payment method."
    module_permission = "payments.view"
    columns = (Col("method", "Payment method"), Col("received", "Received (LKR)", "money"), Col("refunded", "Refunded (LKR)", "money"), Col("net", "Net collected (LKR)", "money"))

    def rows(self):
        base = finance.ledger(self.date_from, self.date_to)
        out = []
        for value, label in Method.choices[:3]:
            received = base.filter(kind=Kind.PAYMENT, method=value).aggregate(t=Sum("amount"))["t"] or ZERO
            refunded = base.filter(kind=Kind.REFUND, method=value).aggregate(t=Sum("amount"))["t"] or ZERO
            out.append({"method": label, "received": received, "refunded": refunded, "net": received - refunded})
        return out

    def summary(self):
        s = finance.summary(self.date_from, self.date_to)
        return [("Invoiced in period", s["invoiced"], "money"), ("Collected (received)", s["gross"], "money"), ("Refunds", s["refunds"], "money"),
                ("Revenue (net collected)", s["net"], "money"), ("Outstanding now (receivable)", s["outstanding"], "money")]

    def chart(self):
        rows = [r for r in self.rows() if r["net"]]
        return {"type": "doughnut", "labels": [r["method"] for r in rows], "datasets": [{"label": "Net collected", "data": [float(r["net"]) for r in rows]}]} if rows else None


class ExpenseReport(Report):
    key, title, icon = "expenses", "Expenses", "receipt-cutoff"
    description = "Operating expenses in the period (void expenses are excluded)."
    module_permission = "expenses.view"
    columns = (Col("date", "Date", "date"), Col("category", "Category"), Col("description", "Description"), Col("supplier", "Supplier"),
               Col("reference", "Reference"), Col("amount", "Amount (LKR)", "money"), Col("entered_by", "Entered by"), Col("source", "Source"))

    def _qs(self):
        return Expense.objects.filter(voided_at__isnull=True, expense_date__gte=self.date_from, expense_date__lte=self.date_to).select_related("category", "supplier", "entered_by")

    def rows(self):
        return [{"date": e.expense_date, "category": e.category.name, "description": e.description, "supplier": e.supplier.name if e.supplier else "", "reference": e.reference,
                 "amount": e.amount, "entered_by": e.entered_by.display_name if e.entered_by else "", "source": e.source_label or "Manual"} for e in self._qs().order_by("expense_date", "pk")]

    def summary(self):
        by_cat = self._qs().values("category__name").annotate(t=Sum("amount")).order_by("-t")
        return [("Total expenses", sum((c["t"] for c in by_cat), ZERO), "money")] + [(f"• {c['category__name']}", c["t"], "money") for c in by_cat]

    def chart(self):
        by_cat = list(self._qs().values("category__name").annotate(t=Sum("amount")).order_by("-t"))
        return {"type": "doughnut", "labels": [c["category__name"] for c in by_cat], "datasets": [{"label": "Expenses", "data": [float(c["t"]) for c in by_cat]}]} if by_cat else None


class RevenueVsExpense(Report):
    key, title, icon = "revenue-vs-expense", "Revenue versus expenses", "graph-up-arrow"
    description = "Monthly revenue (net collected), expenses and estimated operating profit (cash basis)."
    module_permission = "expenses.view"
    columns = (Col("month", "Month"), Col("revenue", "Revenue collected (LKR)", "money"), Col("expenses", "Expenses (LKR)", "money"), Col("profit", "Estimated operating profit (LKR)", "money"))

    @classmethod
    def allowed(cls, user):
        return super().allowed(user) and user.has_gym_perm("payments.view")

    def _series(self):
        return finance.monthly_series(self.date_from, self.date_to)

    def rows(self):
        return [{"month": r["label"], "revenue": r["revenue"], "expenses": r["expenses"], "profit": r["profit"]} for r in self._series()]

    def summary(self):
        s = self._series()
        revenue, spent = sum((r["revenue"] for r in s), ZERO), sum((r["expenses"] for r in s), ZERO)
        return [("Revenue collected", revenue, "money"), ("Expenses", spent, "money"), ("Estimated operating profit", revenue - spent, "money")]

    def chart(self):
        s = self._series()
        return {"type": "bar", "labels": [r["label"] for r in s], "datasets": [{"label": "Revenue", "data": [float(r["revenue"]) for r in s]},
                                                                             {"label": "Expenses", "data": [float(r["expenses"]) for r in s]}]} if s else None


class AppointmentReport(Report):
    key, title, icon = "appointments", "Training appointments", "calendar-event"
    description = "Personal-training sessions that fall in the period."
    module_permission = "appointments.view"
    status_options = (("scheduled", "Scheduled"), ("completed", "Completed"), ("cancelled", "Cancelled"), ("no_show", "No-show"))
    columns = (Col("when", "Date & time", "datetime"), Col("member", "Member"), Col("trainer", "Trainer"), Col("minutes", "Minutes", "int"), Col("status", "Status"),
               Col("fee", "Fee (LKR)", "money"), Col("paid", "Paid (LKR)", "money"))

    def _qs(self):
        start, end = day_range(self)
        qs = Appointment.objects.filter(start__gte=start, start__lt=end).select_related("member", "trainer")
        return qs.filter(status=self.status) if self.status else qs

    def rows(self):
        billed = {a.pk: a for a in with_billing(self._qs(), "fee")}
        return [{"when": a.start, "member": f"{a.member.full_name} ({a.member.member_id})", "trainer": a.trainer.full_name, "minutes": a.duration_minutes,
                 "status": a.get_status_display(), "fee": a.fee, "paid": billed[a.pk].net_paid} for a in self._qs().order_by("start")]

    def summary(self):
        counts = {r["status"]: r["n"] for r in self._qs().values("status").annotate(n=Count("pk"))}
        return [(label, counts.get(v, 0), "int") for v, label in self.status_options]

    def chart(self):
        by_trainer = list(self._qs().values("trainer__full_name").annotate(n=Count("pk")).order_by("-n"))
        return {"type": "bar", "labels": [r["trainer__full_name"] for r in by_trainer], "datasets": [{"label": "Sessions", "data": [r["n"] for r in by_trainer]}]} if by_trainer else None


class EquipmentMaintenanceReport(Report):
    key, title, icon = "equipment-maintenance", "Equipment maintenance", "tools"
    description = "Maintenance and repair records in the period, with costs."
    module_permission = "equipment.view"
    columns = (Col("date", "Date", "date"), Col("code", "Code"), Col("equipment", "Equipment"), Col("kind", "Type"), Col("description", "Description"),
               Col("technician", "Technician"), Col("cost", "Cost (LKR)", "money"), Col("next_due", "Next due", "date"))

    def _qs(self):
        return MaintenanceRecord.objects.filter(performed_on__gte=self.date_from, performed_on__lte=self.date_to).select_related("equipment")

    def rows(self):
        return [{"date": r.performed_on, "code": r.equipment.code, "equipment": r.equipment.name, "kind": r.get_kind_display(), "description": r.description,
                 "technician": r.technician, "cost": r.cost, "next_due": r.next_due} for r in self._qs().order_by("performed_on", "pk")]

    def summary(self):
        qs = self._qs()
        today = timezone.localdate()
        return [("Records", qs.count(), "int"), ("Total maintenance cost", qs.aggregate(t=Sum("cost"))["t"] or ZERO, "money"),
                ("…of which repairs", qs.filter(kind="repair").aggregate(t=Sum("cost"))["t"] or ZERO, "money"),
                ("Overdue for maintenance now", Equipment.objects.filter(next_maintenance_date__lt=today).count(), "int"),
                ("Out of service / under maintenance now", Equipment.objects.filter(status__in=[EquipmentStatus.OUT_OF_SERVICE, EquipmentStatus.MAINTENANCE]).count(), "int")]


class InventoryReport(Report):
    key, title, icon = "inventory", "Inventory and low stock", "boxes"
    description = "Current stock levels with low-stock flags, and stock bought and used in the period."
    module_permission = "inventory.view"
    enabled_setting = "INVENTORY_ENABLED"
    status_options = (("low", "Low or out of stock"), ("ok", "Healthy stock"))
    columns = (Col("sku", "SKU"), Col("name", "Item"), Col("category", "Category"), Col("quantity", "In stock", "int"), Col("min_stock", "Minimum", "int"),
               Col("state", "Stock level"), Col("bought", "Bought in period", "int"), Col("used", "Used in period", "int"), Col("value", "Stock value (LKR)", "money"))

    def rows(self):
        start, end = day_range(self)
        period = Q(movements__created_at__gte=start, movements__created_at__lt=end)
        items = InventoryItem.objects.filter(is_active=True).annotate(
            bought=Sum("movements__delta", filter=period & Q(movements__kind=MovementKind.PURCHASE)),
            used=Sum("movements__delta", filter=period & Q(movements__kind=MovementKind.USAGE))).order_by("name")
        out = []
        for i in items:
            state = {"out": "Out of stock", "low": "Low", "ok": "OK"}[i.stock_state]
            if self.status == "low" and i.stock_state == "ok" or self.status == "ok" and i.stock_state != "ok":
                continue
            out.append({"sku": i.sku, "name": i.name, "category": i.get_category_display(), "quantity": i.quantity, "min_stock": i.min_stock, "state": state,
                        "bought": i.bought or 0, "used": -(i.used or 0), "value": (i.last_unit_cost * i.quantity) if i.last_unit_cost is not None else None})
        return out

    def summary(self):
        items = list(InventoryItem.objects.filter(is_active=True))
        value = sum((i.last_unit_cost * i.quantity for i in items if i.last_unit_cost is not None), ZERO)
        return [("Active items", len(items), "int"), ("Low or out of stock", sum(1 for i in items if i.is_low), "int"), ("Estimated stock value (at last purchase price)", value, "money")]


REPORTS = [MemberRegistrations, MembershipStatusReport, RenewalReport, PlanPopularity, AttendanceReport, MemberAttendance, PaymentReport, OutstandingReport,
           RevenueReport, ExpenseReport, RevenueVsExpense, AppointmentReport, EquipmentMaintenanceReport, InventoryReport]
REGISTRY = {r.key: r for r in REPORTS}

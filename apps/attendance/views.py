from datetime import datetime, timedelta
from io import BytesIO

import qrcode
from django.contrib import messages
from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.functions import ExtractHour
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.listing import resolve_sort, sort_columns
from apps.core.models import ActivityLog
from apps.members.models import Member, new_qr_token, phone_search_term
from apps.memberships.queries import MEMBER_STATUS_LABELS, members_with_status
from apps.trainers.access import scope_by_member, scope_members

from . import services
from .models import Attendance
from .services import CheckInDenied


def member_search(q, user, limit=10):
    """Search by member ID, name or phone number."""
    q = q.strip()
    cond = Q(member_id__icontains=q) | Q(full_name__icontains=q)
    digits = phone_search_term(q)
    if digits:
        cond |= Q(phone__contains=digits)
    open_visit = Attendance.objects.filter(member=OuterRef("pk"), check_out__isnull=True)
    qs = members_with_status(scope_members(user, Member.objects.filter(cond))).annotate(inside=Exists(open_visit))
    return qs.order_by("full_name")[:limit]


def day_bounds(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()), tz)
    return start, start + timedelta(days=1)


class DeskView(GymPermissionMixin, TemplateView):
    """Front desk: find a member, check them in or out, see who is in the gym."""

    required_permission = "attendance.view"
    template_name = "attendance/desk.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()
        start, end = day_bounds(timezone.localdate())
        inside = scope_by_member(self.request.user, Attendance.objects.filter(check_out__isnull=True).select_related("member", "membership__plan"))
        ctx.update(
            q=q,
            results=member_search(q, self.request.user) if q else [],
            status_labels=MEMBER_STATUS_LABELS,
            inside=inside.order_by("check_in"),
            today_count=Attendance.objects.filter(check_in__gte=start, check_in__lt=end).count(),
            today_members=Attendance.objects.filter(check_in__gte=start, check_in__lt=end).values("member").distinct().count(),
        )
        return ctx


class CheckInView(GymPermissionMixin, View):
    required_permission = "attendance.manage"
    http_method_names = ["post"]

    def post(self, request, member_pk):
        member = get_object_or_404(Member, pk=member_pk)
        try:
            visit = services.check_in(member, request.user)
            messages.success(request, f"{member.full_name} checked in at {timezone.localtime(visit.check_in):%H:%M}.")
        except CheckInDenied as exc:
            messages.error(request, f"Check-in refused for {member.full_name}: {exc}")
        return redirect(request.POST.get("next_url") if request.POST.get("next_url", "").startswith("/attendance/") else "attendance:desk")


class CheckOutView(GymPermissionMixin, View):
    required_permission = "attendance.manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        visit = get_object_or_404(Attendance.objects.select_related("member"), pk=pk)
        try:
            visit = services.check_out(visit, request.user)
            messages.success(request, f"{visit.member.full_name} checked out ({_fmt_duration(visit.duration)}).")
        except CheckInDenied as exc:
            messages.error(request, str(exc))
        return redirect("attendance:desk")


def _fmt_duration(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


class ScanView(GymPermissionMixin, View):
    """QR kiosk: a scanner (or camera) submits the token; a scan toggles check-in / check-out."""

    required_permission = "attendance.manage"
    template_name = "attendance/scan.html"

    def get(self, request):
        return render(request, self.template_name, {"ok": None})

    def post(self, request):
        ctx = {}
        try:
            action, visit, member = services.scan_qr(request.POST.get("code", ""), request.user)
            ctx.update(ok=True, action=action, member=member, visit=visit)
        except CheckInDenied as exc:
            ctx.update(ok=False, error=str(exc))
            # Name the member on refusals caused by rules (not for unrecognised codes).
            member = Member.objects.filter(qr_token=services.parse_qr(request.POST.get("code", ""))).first()
            if member:
                ctx["member"] = member
        return render(request, self.template_name, ctx, status=200 if ctx["ok"] else 422)


class HistoryView(GymPermissionMixin, ListView):
    required_permission = "attendance.view"
    template_name = "attendance/history.html"
    paginate_by = 25
    SORTS = {"time": "check_in", "member": "member__full_name"}
    COLUMNS = [("time", "Check-in"), ("member", "Member")]

    def filtered(self, qs):
        g = self.request.GET
        if g.get("q", "").strip():
            q = g["q"].strip()
            digits = phone_search_term(q)
            cond = Q(member__full_name__icontains=q) | Q(member__member_id__icontains=q)
            if digits:
                cond |= Q(member__phone__contains=digits)
            qs = qs.filter(cond)
        d_from, d_to = parse_date(g.get("from", "")), parse_date(g.get("to", ""))
        if d_from:
            qs = qs.filter(check_in__gte=day_bounds(d_from)[0])
        if d_to:
            qs = qs.filter(check_in__lt=day_bounds(d_to)[1])
        if g.get("method") in ("manual", "qr"):
            qs = qs.filter(method=g["method"])
        return qs

    def base_queryset(self):
        return scope_by_member(self.request.user, Attendance.objects.select_related("member", "membership__plan"))

    def get_queryset(self):
        qs = self.filtered(self.base_queryset())
        self.sort, self.direction, order = resolve_sort(self.request, self.SORTS, "time")
        if "sort" not in self.request.GET:
            self.direction, order = "desc", ["-check_in", "-pk"]
        return qs.order_by(*order)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        ctx.update(
            q=g.get("q", ""), date_from=g.get("from", ""), date_to=g.get("to", ""), method=g.get("method", ""),
            columns={c["key"]: c for c in sort_columns(self.request, self.COLUMNS, self.sort, self.direction)},
            filtered_on=any(g.get(k) for k in ("q", "from", "to", "method")),
        )
        return ctx


class MemberAttendanceView(HistoryView):
    template_name = "attendance/member_history.html"

    def base_queryset(self):
        self.member = get_object_or_404(scope_members(self.request.user, Member.objects.all()), pk=self.kwargs["member_pk"])
        return Attendance.objects.filter(member=self.member).select_related("membership__plan")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        total = self.filtered(Attendance.objects.filter(member=self.member))
        ctx.update(member=self.member, visit_count=total.count(), days_count=len({timezone.localtime(v).date() for v in total.values_list("check_in", flat=True)}))
        return ctx


class DailyReportView(GymPermissionMixin, TemplateView):
    required_permission = "attendance.view"
    template_name = "attendance/report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        day = parse_date(self.request.GET.get("date", "")) or timezone.localdate()
        start, end = day_bounds(day)
        visits = scope_by_member(self.request.user, Attendance.objects.filter(check_in__gte=start, check_in__lt=end).select_related("member", "membership__plan"))
        tz = timezone.get_current_timezone()
        hours = dict(
            visits.annotate(h=ExtractHour("check_in", tzinfo=tz)).values_list("h").annotate(n=Count("pk")).order_by("h")
        )
        peak = max(hours.values(), default=0)
        durations = [v.duration for v in visits if v.check_out]
        ctx.update(
            day=day, prev_day=day - timedelta(days=1), next_day=day + timedelta(days=1), is_today=day == timezone.localdate(),
            visits=visits.order_by("check_in"), total=visits.count(),
            unique=visits.values("member").distinct().count(), still_inside=visits.filter(check_out__isnull=True).count(),
            qr_count=visits.filter(method="qr").count(),
            average=_fmt_duration(sum(durations, timedelta()) / len(durations)) if durations else "—",
            hourly=[{"hour": f"{h:02d}:00", "n": hours.get(h, 0), "pct": round(hours.get(h, 0) * 100 / peak) if peak else 0}
                    for h in range(24) if hours.get(h) or 6 <= h <= 22],
        )
        return ctx


# ------------------------------------------------------------------------------------------- QR cards
class MemberQRView(GymPermissionMixin, TemplateView):
    required_permission = "attendance.manage"
    template_name = "attendance/qr_card.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(member=get_object_or_404(Member, pk=self.kwargs["member_pk"]), **kwargs)


class MemberQRImageView(GymPermissionMixin, View):
    required_permission = "attendance.manage"

    def get(self, request, member_pk):
        member = get_object_or_404(Member, pk=member_pk)
        img = qrcode.make(services.qr_payload(member), box_size=8, border=2)
        buf = BytesIO()
        img.save(buf, "PNG")
        response = HttpResponse(buf.getvalue(), content_type="image/png")
        response["Cache-Control"] = "private, no-store"  # the image contains the member's check-in secret
        return response


class RegenerateQRView(GymPermissionMixin, View):
    required_permission = "attendance.manage"
    http_method_names = ["post"]

    def post(self, request, member_pk):
        member = get_object_or_404(Member, pk=member_pk)
        member.qr_token = new_qr_token()
        member.save(update_fields=["qr_token", "updated_at"])
        ActivityLog.record(request.user, "member_qr_reset", f"Reset check-in QR code of {member.full_name} ({member.member_id})")
        messages.success(request, "A new QR code was generated. The old one no longer works.")
        return redirect("attendance:qr_card", member_pk=member.pk)

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.mixins import GymPermissionMixin
from apps.core.models import ActivityLog

from . import base
from .definitions import REGISTRY, REPORTS

PAGE_SIZE = 25


class ReportIndexView(GymPermissionMixin, TemplateView):
    required_permission = "reports.view"
    template_name = "reports/index.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(reports=[r for r in REPORTS if r.allowed(self.request.user)], **kwargs)


class ReportView(GymPermissionMixin, View):
    """One report as HTML, printable page, CSV or PDF. Access needs `reports.view` plus the permission of the module whose
    data the report shows (e.g. expenses.view), so financial reports stay restricted."""

    required_permission = "reports.view"

    def get(self, request, key):
        cls = REGISTRY.get(key)
        if cls is None:
            raise Http404
        if not cls.allowed(request.user):
            if cls.enabled_setting:
                from django.conf import settings

                if not getattr(settings, cls.enabled_setting):
                    raise Http404
            raise PermissionDenied
        report = cls(request.user, request.GET)
        rows = report.filtered_rows()
        export = request.GET.get("export")
        slug = f"{cls.key}-{report.date_from:%Y%m%d}-{report.date_to:%Y%m%d}" if cls.uses_dates else cls.key
        if export == "csv":
            ActivityLog.record(request.user, "report_exported", f"Exported {cls.title} (CSV)")
            response = HttpResponse(base.to_csv(report, rows), content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{slug}.csv"'
            return response
        if export == "pdf":
            ActivityLog.record(request.user, "report_exported", f"Exported {cls.title} (PDF)")
            response = HttpResponse(base.to_pdf(report, rows), content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{slug}.pdf"'
            return response
        ctx = {"report": report, "summary": report.summary(), "chart": report.chart(), "total_rows": len(rows)}
        if request.GET.get("print"):
            return render(request, "reports/print.html", {**ctx, "rows": rows})
        page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        params.pop("export", None)
        params.pop("print", None)
        return render(request, "reports/report.html", {**ctx, "page_obj": page, "rows": page.object_list, "query": params.urlencode()})

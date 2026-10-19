"""Report framework: a report declares columns and produces rows/summary from the database. The views add
date filters, search, pagination, CSV, printable and PDF output uniformly."""
import csv
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO, StringIO

from django.conf import settings

from apps.core.gym import gym
from django.utils import timezone
from django.utils.dateparse import parse_date

MAX_RANGE_DAYS = 366 * 5


class Col:
    def __init__(self, key, label, kind="text"):
        self.key, self.label, self.kind = key, label, kind  # kind: text | int | money | date | datetime | percent


class Report:
    do_not_call_in_templates = True  # the index template lists the classes themselves
    key = ""
    title = ""
    description = ""
    icon = "bar-chart"
    module_permission = None       # in addition to reports.view
    uses_dates = True
    columns = ()
    group_options = ()             # e.g. (("day", "Daily"), ("month", "Monthly"))
    status_options = ()
    needs_member = False
    enabled_setting = None         # e.g. "INVENTORY_ENABLED"

    def __init__(self, user, params):
        self.user, self.params = user, params
        today = timezone.localdate()
        d_from, d_to = parse_date(params.get("from", "")), parse_date(params.get("to", ""))
        self.date_to = d_to or today
        self.date_from = d_from or self.date_to.replace(day=1)
        if self.date_from > self.date_to:
            self.date_from, self.date_to = self.date_to, self.date_from
        if (self.date_to - self.date_from).days > MAX_RANGE_DAYS:
            self.date_from = self.date_to.replace(year=self.date_to.year - 5)
        self.q = params.get("q", "").strip()
        self.group = params.get("group") if params.get("group") in dict(self.group_options) else (self.group_options[0][0] if self.group_options else "")
        self.status = params.get("status", "") if params.get("status", "") in dict(self.status_options) else ""
        self.member_query = params.get("member", "").strip()

    @classmethod
    def allowed(cls, user):
        if enabled := cls.enabled_setting:
            if not getattr(settings, enabled):
                return False
        return user.has_gym_perm("reports.view") and (cls.module_permission is None or user.has_gym_perm(cls.module_permission))

    # --- to implement
    def rows(self):
        raise NotImplementedError

    def summary(self):
        """[(label, value, kind)]"""
        return []

    def chart(self):
        """{"type": "bar"|"line"|"doughnut", "labels": [...], "datasets": [{"label": str, "data": [numbers]}]} or None"""
        return None

    # --- shared
    def filtered_rows(self):
        rows = self.rows()
        if self.q:
            needle = self.q.lower()
            rows = [r for r in rows if any(needle in str(v).lower() for v in r.values() if v is not None)]
        return rows

    @property
    def period_label(self):
        return f"{self.date_from:%b %d, %Y} – {self.date_to:%b %d, %Y}" if self.uses_dates else "As of today"


def fmt(value, kind):
    """Plain-text formatting used by CSV / PDF (HTML uses the template filter with the same rules)."""
    if value is None or value == "":
        return ""
    if kind == "money":
        return f"{Decimal(value):,.2f}"
    if kind == "date":
        return value.strftime("%Y-%m-%d") if isinstance(value, (date, datetime)) else str(value)
    if kind == "datetime":
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else str(value)
    if kind == "percent":
        return f"{value}%"
    return str(value)


def _safe(cell):
    """Neutralise spreadsheet formula injection (names or notes starting with = + - @)."""
    return "'" + cell if cell[:1] in ("=", "+", "-", "@") and not cell.replace(",", "").replace(".", "").lstrip("-+").isdigit() else cell


def to_csv(report, rows):
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow([report.title])
    writer.writerow([f"Period: {report.period_label}", f"Generated: {timezone.localtime():%Y-%m-%d %H:%M}"])
    for label, value, kind in report.summary():
        writer.writerow([label, fmt(value, kind)])
    writer.writerow([])
    writer.writerow([c.label for c in report.columns])
    for r in rows:
        writer.writerow([_safe(fmt(r.get(c.key), c.kind)) for c in report.columns])
    return "﻿" + buf.getvalue()  # BOM so Excel reads UTF-8


def to_pdf(report, rows):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=report.title, author=gym().name)
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=7.5, leading=9)
    head = ParagraphStyle("h", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    story = [Paragraph(escape(f"{gym().name} – {report.title}"), styles["Title"]),
             Paragraph(escape(f"{report.period_label} · generated {timezone.localtime():%Y-%m-%d %H:%M}"), styles["Normal"]), Spacer(1, 4 * mm)]
    summary = report.summary()
    if summary:
        t = Table([[Paragraph(escape(label), cell), Paragraph(escape(fmt(value, kind)), cell)] for label, value, kind in summary], colWidths=[70 * mm, 45 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
        story += [t, Spacer(1, 4 * mm)]
    data = [[Paragraph(escape(c.label), head) for c in report.columns]]
    for r in rows:
        data.append([Paragraph(escape(fmt(r.get(c.key), c.kind)), cell) for c in report.columns])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12151c")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f5f8")]),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(table if rows else Paragraph("No records for this selection.", styles["Normal"]))
    doc.build(story)
    return buf.getvalue()

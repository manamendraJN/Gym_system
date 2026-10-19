"""PDF receipt rendering (reportlab). The same data dict feeds the printable HTML page."""
from io import BytesIO

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from .billing import balance_as_of
from .models import Kind, Status


def receipt_data(payment):
    """Everything shown on a receipt, in one place."""
    return {
        "payment": payment,
        "member": payment.member,
        "item_label": payment.item_label,
        "item_period": payment.item_period,
        "item_kind": "Personal training" if payment.is_appointment else "Membership",
        "is_refund": payment.kind == Kind.REFUND,
        "is_valid": payment.status == Status.COMPLETED,
        "gym_name": settings.GYM_NAME,
        **balance_as_of(payment),
    }


def _lkr(value):
    return f"LKR {value:,.2f}"


def render_pdf(payment):
    d = receipt_data(payment)
    p, m = payment, d["member"]
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"{p.receipt_number}", author=settings.GYM_NAME)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=styles["Title"], fontSize=17, alignment=0, spaceAfter=2)
    small = ParagraphStyle("s", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    body = ParagraphStyle("b", parent=styles["Normal"], fontSize=9.5, leading=13)

    def para(text, style=body):
        return Paragraph(escape(str(text)), style)

    story = [
        para(d["gym_name"], title),
        para("REFUND RECEIPT" if d["is_refund"] else "PAYMENT RECEIPT", small),
        Spacer(1, 4 * mm),
    ]
    if not d["is_valid"]:
        story += [para(f"STATUS: {p.get_status_display().upper()} – this document is not a valid receipt.",
                       ParagraphStyle("w", parent=body, textColor=colors.HexColor("#b02a37"), fontName="Helvetica-Bold")),
                  Spacer(1, 3 * mm)]
    rows = [
        ["Receipt no.", p.receipt_number], ["Transaction ref.", p.reference],
        ["Date", f"{p.paid_on:%d %b %Y}"], ["Method", p.get_method_display()],
    ]
    if p.external_reference:
        rows.append(["Bank/terminal ref.", p.external_reference])
    rows += [["Member", f"{m.full_name} ({m.member_id})"], ["Phone", m.phone],
             [d["item_kind"], f"{d['item_label']} · {d['item_period']}"]]
    if p.kind == Kind.REFUND and p.refund_of_id:
        rows.append(["Refund of", p.refund_of.receipt_number])
        rows.append(["Reason", p.reason])
    table = Table([[para(k, small), para(v)] for k, v in rows], colWidths=[38 * mm, None])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [table, Spacer(1, 4 * mm)]
    figures = [
        [f"{d['item_kind']} charge (amount due)", _lkr(d["amount_due"])],
        [("Refunded" if d["is_refund"] else "Amount paid (this receipt)"), _lkr(p.amount)],
        ["Total paid to date (net of refunds)", _lkr(d["net_paid"])],
        ["Outstanding balance", _lkr(d["outstanding"])],
    ]
    t2 = Table([[para(k), para(v, ParagraphStyle("r", parent=body, alignment=2))] for k, v in figures], colWidths=[None, 40 * mm])
    t2.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.grey), ("LINEABOVE", (0, 3), (-1, 3), 0.6, colors.grey),
                            ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [t2, Spacer(1, 6 * mm)]
    if p.recorded_by:
        story.append(para(f"Recorded by {p.recorded_by.display_name} on {p.created_at:%d %b %Y %H:%M}", small))
    story.append(para("Balances are shown as at the time of this receipt.", small))
    doc.build(story)
    return buf.getvalue()

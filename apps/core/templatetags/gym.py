from django import template

register = template.Library()


@register.filter
def can(user, code):
    """{% if request.user|can:"users.view" %} – mirrors the server-side permission check."""
    return user.is_authenticated and user.has_gym_perm(code)


BADGE_CLASSES = {
    "active": "success", "scheduled": "info", "suspended": "warning", "expired": "danger",
    "cancelled": "secondary", "superseded": "secondary", "inactive": "dark",
    "completed": "success", "pending": "warning", "failed": "danger", "voided": "secondary",
    "unpaid": "danger", "partial": "warning", "paid": "success", "partially_refunded": "info", "refunded": "secondary", "none": "light border text-muted",
}


@register.filter
def badge(key):
    """Bootstrap colour for a membership/member status key (consistent across all pages)."""
    return BADGE_CLASSES.get(key, "secondary")


@register.filter
def member_status(key):
    from apps.memberships.queries import MEMBER_STATUS_LABELS

    return MEMBER_STATUS_LABELS.get(key, key)


@register.filter
def duration(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


@register.filter
def clock(seconds):
    """Seconds -> 'mm:ss' (e.g. 90 -> '01:30'); empty values read as 00:00."""
    total = int(seconds or 0)
    return f"{total // 60:02d}:{total % 60:02d}"


@register.filter
def duration_clock(delta):
    """timedelta -> 'mm:ss', or 'h:mm:ss' from one hour; empty -> '–'."""
    if delta is None:
        return "–"
    total = max(0, int(delta.total_seconds()))
    h, rem = divmod(total, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60:02d}:{rem % 60:02d}"


@register.filter
def payment_status(key):
    from apps.payments.billing import PAYMENT_STATUS_LABELS

    return PAYMENT_STATUS_LABELS.get(key, key)


@register.filter
def report_cell(value, kind):
    """Format a report value for HTML (CSV/PDF use apps.reports.base.fmt)."""
    from datetime import date, datetime

    from django.utils import timezone

    if value is None or value == "":
        return "—"
    if kind == "money":
        return f"{value:,.2f}"
    if kind == "datetime" and isinstance(value, datetime):
        return f"{timezone.localtime(value):%b %d, %Y %H:%M}"
    if kind == "date" and isinstance(value, date):
        return f"{value:%b %d, %Y}"
    if kind == "percent":
        return f"{value}%"
    if kind == "int":
        return f"{value:,}"
    if isinstance(value, date):
        return f"{value:%b %d, %Y}"
    return str(value)


@register.filter
def report_value(value, kind):
    return report_cell(value, kind)


@register.filter
def lkr(value):
    """Format an amount in Sri Lankan rupees."""
    if value in (None, ""):
        return "—"
    from django.template.defaultfilters import floatformat

    return f"LKR {floatformat(value, '2g')}"


@register.filter
def get_item(mapping, key):
    return mapping.get(key)


@register.simple_tag(takes_context=True)
def querystring(context, **kwargs):
    """Current GET params with overrides (used by pagination links)."""
    params = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()

"""Gym-wide settings (name, contact details, receipt footer), editable in the application.

The environment variable GYM_NAME is only the initial default; once a Gym Manager saves the settings page the database
value is used everywhere (layout, receipts, e-mails, reports)."""
from types import SimpleNamespace

from django.conf import settings
from django.db import DatabaseError


def gym():
    """The current gym settings. Never raises: falls back to environment defaults (e.g. before migrations have run)."""
    from .models import GymSettings

    try:
        obj = GymSettings.objects.filter(pk=1).first()
    except DatabaseError:  # table not created yet
        obj = None
    if obj is not None:
        return obj
    return SimpleNamespace(name=settings.GYM_NAME, tagline="", address="", phone="", email="", receipt_footer="")


def gym_name():
    return gym().name

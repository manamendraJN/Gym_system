from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from apps.core.models import ActivityLog


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    ActivityLog.record(user, "login", "Signed in")


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs):
    if user is not None:
        ActivityLog.record(user, "logout", "Signed out")

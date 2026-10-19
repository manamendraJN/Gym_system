"""Optional Celery configuration for scheduled notifications.

Not needed for the application to work: the same jobs run from cron with `python manage.py run_scheduled_notifications`.
To use Celery install `requirements-celery.txt`, set CELERY_BROKER_URL (e.g. redis://localhost:6379/0) and start:

    celery -A config worker -l info
    celery -A config beat -l info
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("gym_system")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["apps.notifications"])

app.conf.beat_schedule = {
    # Generates expiry / payment / appointment / maintenance / low-stock notifications and announcements; idempotent.
    "scheduled-notifications": {"task": "notifications.run_scheduled", "schedule": crontab(minute=0)},          # hourly
    # Retries queued e-mail/SMS/WhatsApp deliveries between the hourly runs.
    "deliver-queued-messages": {"task": "notifications.deliver_queued", "schedule": crontab(minute="*/10")},
}

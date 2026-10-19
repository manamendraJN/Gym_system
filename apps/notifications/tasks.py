"""Scheduled work. These are plain functions, so they run without any extra infrastructure via
`python manage.py run_scheduled_notifications` (cron / Task Scheduler). If Celery is installed they are also
registered as Celery tasks (see config/celery.py and the README for the broker setup)."""
from . import delivery, services


def run_scheduled_notifications():
    return services.run_all()


def deliver_queued_messages():
    return delivery.process_queue()


try:  # Celery is optional
    from celery import shared_task

    run_scheduled_notifications_task = shared_task(name="notifications.run_scheduled")(run_scheduled_notifications)
    deliver_queued_messages_task = shared_task(name="notifications.deliver_queued")(deliver_queued_messages)
except ImportError:  # pragma: no cover
    run_scheduled_notifications_task = deliver_queued_messages_task = None

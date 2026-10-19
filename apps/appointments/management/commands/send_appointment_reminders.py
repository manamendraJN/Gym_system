from django.core.management.base import BaseCommand

from apps.appointments.services import send_due_reminders


class Command(BaseCommand):
    help = "Send reminders for training sessions starting soon (run every 15–60 minutes from cron / Task Scheduler)."

    def handle(self, *args, **options):
        self.stdout.write(f"{send_due_reminders()} reminder(s) sent.")

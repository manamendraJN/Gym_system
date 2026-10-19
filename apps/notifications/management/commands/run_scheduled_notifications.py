from django.core.management.base import BaseCommand

from apps.notifications.services import run_all


class Command(BaseCommand):
    help = ("Generate all scheduled notifications (membership expiry, payment due, appointment reminders, equipment maintenance, "
            "low stock, announcements) and attempt queued external deliveries. Safe to run repeatedly; run hourly or daily.")

    def handle(self, *args, **options):
        results = run_all()
        deliveries = results.pop("deliveries")
        for name, count in results.items():
            self.stdout.write(f"{name.replace('_', ' ')}: {count} notification(s) created")
        summary = ", ".join(f"{count} {status}" for status, count in deliveries.items() if count)
        self.stdout.write("external deliveries attempted: " + (summary or "none"))

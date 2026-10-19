from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.demo import DEMO_PASSWORD
from apps.core.sample import SampleDataError, load_sample_data


class Command(BaseCommand):
    help = "Load realistic sample data (members, plans, payments, attendance, staff logins ...) for development. Development only."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Refusing to load sample data while DEBUG is off (this looks like a production setup). "
                               "Sample data is for development machines only.")
        try:
            result = load_sample_data()
        except SampleDataError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Sample data loaded: {result['records']} records tracked for later removal."))
        self.stdout.write(f"Sample logins (password: {DEMO_PASSWORD}): demo_manager, demo_reception, demo_accountant, demo_trainer, demo_alice, demo_bob")
        self.stdout.write("See SAMPLE_DATA_GUIDE.md. Remove it all later with: python manage.py cleanup_sample_data")

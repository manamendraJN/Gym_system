from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import SampleRecord
from apps.core.sample import cleanup_sample_data


class Command(BaseCommand):
    help = "Remove ONLY the records created by load_sample_data (tracked in SampleRecord). Real data and the Super Admin are never touched."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Refusing to run cleanup while DEBUG is off (this looks like a production setup).")
        count = SampleRecord.objects.count()
        if not count:
            self.stdout.write("No tracked sample data found; nothing to remove.")
            return
        self.stdout.write(f"This will delete the {count} records created by load_sample_data (sample members, their payments, attendance, "
                          "sample logins and so on). Records of real data, the Super Admin and any other existing account are kept.")
        if not options["yes"] and input("Type 'yes' to continue: ").strip().lower() != "yes":
            self.stdout.write("Cancelled. Nothing was deleted.")
            return
        deleted, preserved = cleanup_sample_data()
        self.stdout.write(self.style.SUCCESS(f"Removed {deleted} sample records."))
        if preserved:
            self.stdout.write(self.style.WARNING(f"Kept {len(preserved)} record(s) because real data still depends on them: {', '.join(preserved[:20])}"))

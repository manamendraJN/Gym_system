from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Deprecated alias of load_sample_data (kept so old instructions keep working)."

    def handle(self, *args, **options):
        call_command("load_sample_data", stdout=self.stdout)

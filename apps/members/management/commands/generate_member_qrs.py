from django.core.management.base import BaseCommand

from apps.members.models import Member, new_qr_token


class Command(BaseCommand):
    help = "Give every member that has no check-in QR token one. Existing tokens are never changed, so printed codes keep working."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only report how many members are missing a token.")

    def handle(self, *args, **options):
        missing = Member.objects.filter(qr_token__isnull=True) | Member.objects.filter(qr_token="")
        count = missing.count()
        if options["dry_run"]:
            self.stdout.write(f"{count} member(s) without a QR token.")
            return
        for member in missing:
            member.qr_token = new_qr_token()
            member.save(update_fields=["qr_token"])
        self.stdout.write(self.style.SUCCESS(f"Generated QR tokens for {count} member(s)."))

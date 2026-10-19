from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role, User
from apps.core.demo import DEMO_PASSWORD, seed_demo


class Command(BaseCommand):
    help = "Add demo members, payments, sessions, equipment etc. for trying the application locally. Only adds data; refuses to run in production."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Allow running with DEBUG=False (never use on a live system).")

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Refusing to add demo data while DEBUG is off. Use --force only on a throw-away installation.")
        actor = User.objects.filter(role=Role.SUPER_ADMIN, is_active=True).first()
        if actor is None:
            raise CommandError("Create a Super Admin first: python manage.py createsuperuser")
        created = seed_demo(actor)
        if created is None:
            self.stdout.write("Demo data already exists; nothing was added.")
            return
        self.stdout.write(self.style.SUCCESS("Demo data added."))
        self.stdout.write(f"Demo logins (password {DEMO_PASSWORD}): demo_trainer (Trainer), demo_alice and demo_bob (Members).")

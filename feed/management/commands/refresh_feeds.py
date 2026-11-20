from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from feed import refresh


class Command(BaseCommand):
    help = "Fetch sources and score new articles now, for one person or everyone who finished setup."

    def add_arguments(self, parser):
        parser.add_argument("--email", default="")

    def handle(self, *args, email, **options):
        users = User.objects.filter(is_active=True, setup_completed=True)
        if email:
            users = users.filter(email__iexact=email)
            if not users:
                raise CommandError(f"No active user {email} who finished setup.")
        for user in users:
            count = refresh.refresh_user(user)
            self.stdout.write(f"{user.email}: {count} new items scored")

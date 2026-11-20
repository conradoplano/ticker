from django.core.management.base import BaseCommand

from sources import fetch


class Command(BaseCommand):
    help = "Fetch all followed sources that are due (or all of them with --force)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, force, **options):
        fetched, new = fetch.fetch_all(force=force)
        self.stdout.write(self.style.SUCCESS(f"Fetched {fetched} sources, {new} new articles."))

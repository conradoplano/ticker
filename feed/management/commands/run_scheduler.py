"""The background worker: every minute, fetch due sources and run due refreshes. Started by the container's
entrypoint next to the web server (RUN_SCHEDULER=true)."""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from feed import refresh
from sources import fetch

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the scheduler loop: fetch sources and refresh feeds at each person's times."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60, help="Seconds between ticks.")
        parser.add_argument("--once", action="store_true", help="Run one tick and stop.")

    def handle(self, *args, interval, once, **options):
        last_cleanup = None
        while True:
            close_old_connections()
            try:
                summary = refresh.run_due()
                if any(summary.values()):
                    logger.info("Scheduler: %s", summary)
                today = timezone.localdate()
                if last_cleanup != today:
                    removed = fetch.clean_up()
                    last_cleanup = today
                    if removed:
                        logger.info("Scheduler: removed %s old articles", removed)
            except Exception:
                logger.exception("Scheduler tick failed")
            if once:
                return
            time.sleep(interval)

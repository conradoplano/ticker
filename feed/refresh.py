"""
Refreshing feeds: fetch a person's sources, then score what is new. Run by the scheduler at each person's
refresh times, by the Refresh now button, and when someone opens the app after a while away.
"""
import logging
import threading
from datetime import datetime, timedelta

from django.db import close_old_connections
from django.utils import timezone

from accounts.models import User
from sources import fetch
from sources.models import Source

from . import learning, scoring

logger = logging.getLogger(__name__)

# A scheduled refresh that was missed (e.g. the container was down) still runs within this window.
CATCH_UP = timedelta(hours=3)
_running = set()
_lock = threading.Lock()


def refresh_user(user, fetch_first=True):
    """Fetches the person's due sources and scores new articles. Returns the number scored."""
    with _lock:
        if user.pk in _running:
            return 0
        _running.add(user.pk)
    try:
        if fetch_first:
            sources = Source.objects.filter(followers__user=user, followers__active=True, active=True).distinct()
            fetch.fetch_all(sources)
        count, _ = scoring.score_new(user)
        user.last_refresh_at = timezone.now()
        user.save(update_fields=["last_refresh_at"])
        return count
    finally:
        with _lock:
            _running.discard(user.pk)


def refresh_in_background(user):
    """Starts a refresh without making the page wait."""
    def run(pk):
        close_old_connections()
        try:
            person = User.objects.get(pk=pk)
            refresh_user(person)
        except Exception:
            logger.exception("Background refresh failed")
        finally:
            close_old_connections()

    threading.Thread(target=run, args=(user.pk,), daemon=True).start()


def waiting(user):
    """True if the person's sources have articles that are not scored for them yet."""
    return bool(scoring.candidates(user, since=timezone.now() - timedelta(hours=24))[:1])


def parse_time(value):
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None


def current_slot(user, now=None):
    """The latest refresh time that has passed today (local), as "YYYY-MM-DD HH:MM", if within CATCH_UP."""
    local = (now or timezone.now()).astimezone(user.tz)
    passed = []
    for value in user.refresh_times or []:
        moment = parse_time(value)
        if moment is None:
            continue
        slot = datetime.combine(local.date(), moment, tzinfo=user.tz)
        if slot <= local and local - slot <= CATCH_UP:
            passed.append(slot)
    if not passed:
        return None
    return max(passed).strftime("%Y-%m-%d %H:%M")


def run_due(now=None):
    """One scheduler tick: fetch due sources, run due refreshes and weekly learning. Returns a summary."""
    now = now or timezone.now()
    fetched, new = fetch.fetch_all()
    refreshed = learned = 0
    for user in User.objects.filter(is_active=True, setup_completed=True):
        slot = current_slot(user, now)
        if slot and slot != user.last_slot:
            try:
                scoring.score_new(user)
            except Exception:
                logger.exception("Refresh failed for %s", user)
            user.last_slot = slot
            user.last_refresh_at = now
            user.save(update_fields=["last_slot", "last_refresh_at"])
            refreshed += 1
        if learning.learning_due(user, now):
            try:
                learning.learn(user)
                learned += 1
            except Exception as exc:
                logger.info("Learning skipped for %s: %s", user, exc)
                user.learned_at = now  # try again next week rather than every minute
                user.save(update_fields=["learned_at"])
    return {"fetched": fetched, "new": new, "refreshed": refreshed, "learned": learned}

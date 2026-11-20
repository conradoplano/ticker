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
from .models import ArticleScore, Refresh

MIN_SHOWN = 10  # the feed's lowest score (build.MIN_SCORE)

logger = logging.getLogger(__name__)

# A scheduled refresh that was missed (e.g. the container was down) still runs within this window.
CATCH_UP = timedelta(hours=3)
# A refresh still marked as running after this long was cut off (e.g. the container restarted).
STALE = timedelta(minutes=10)


def running(user):
    """The person's refresh that is still going, if any."""
    return Refresh.objects.filter(user=user, finished_at__isnull=True,
                                  started_at__gte=timezone.now() - STALE).first()


# Refresh now also re-checks sources fetched more than this long ago (the background job waits 15 minutes).
MANUAL_RECHECK = timedelta(minutes=2)


def should_fetch(source, job):
    if fetch.due(source):
        return True
    recent = source.last_fetched_at and timezone.now() - source.last_fetched_at < MANUAL_RECHECK
    return job.trigger == Refresh.Trigger.MANUAL and source.error_count == 0 and not recent


def run(job, fetch_first=True):
    """Does the refresh: fetches the person's sources that are due, then ranks the new articles.
    Progress is saved on the job as it goes, for the page to show."""
    user = job.user
    try:
        if fetch_first:
            sources = list(Source.objects.filter(followers__user=user, followers__active=True, active=True).distinct())
            job.sources_total = len(sources)
            job.save(update_fields=["sources_total"])
            for source in sources:
                if should_fetch(source, job):
                    job.articles_fetched += fetch.fetch_source(source)
                job.sources_checked += 1
                job.save(update_fields=["sources_checked", "articles_fetched"])
        job.status = Refresh.Status.RANKING
        job.save(update_fields=["status"])
        job.ranked, _ = scoring.score_new(user)
        added = ArticleScore.objects.filter(user=user, created_at__gte=job.started_at, hidden=False,
                                            score__gte=MIN_SHOWN).select_related("article")
        job.new_items = len(added)
        job.new_stories = len({s.story_group for s in added})
        job.status = Refresh.Status.DONE
    except Exception as exc:
        logger.exception("Refresh failed for %s", user)
        job.status = Refresh.Status.FAILED
        job.error = str(exc)[:300]
    job.finished_at = timezone.now()
    job.save()
    if job.status == Refresh.Status.DONE:
        user.last_refresh_at = job.finished_at
        user.save(update_fields=["last_refresh_at"])
    return job


def refresh_user(user, trigger=Refresh.Trigger.MANUAL):
    """Refreshes now and waits for it (commands, setup). Returns the number of articles ranked."""
    if running(user):
        return 0
    return run(Refresh.objects.create(user=user, trigger=trigger)).ranked


def refresh_in_background(user, trigger=Refresh.Trigger.VISIT):
    """Starts a refresh without making the page wait; returns its job (to show its progress)."""
    existing = running(user)
    if existing:
        return existing
    job = Refresh.objects.create(user=user, trigger=trigger)

    def work(pk):
        close_old_connections()
        try:
            run(Refresh.objects.select_related("user").get(pk=pk))
        except Exception:
            logger.exception("Background refresh failed")
        finally:
            close_old_connections()

    threading.Thread(target=work, args=(job.pk,), daemon=True, name=f"refresh-{job.pk}").start()
    return job


def waiting(user):
    """True if the person's sources have articles that are not scored for them yet."""
    return bool(scoring.candidates(user, since=timezone.now() - timedelta(hours=24))[:1])


def parse_time(value):
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None


def next_update(user, now=None):
    """The next of the person's refresh times (local datetime), or None if they have none."""
    local = (now or timezone.now()).astimezone(user.tz)
    times = sorted(t for t in (parse_time(v) for v in user.refresh_times or []) if t)
    for moment in times:
        slot = datetime.combine(local.date(), moment, tzinfo=user.tz)
        if slot > local:
            return slot
    return datetime.combine(local.date() + timedelta(days=1), times[0], tzinfo=user.tz) if times else None


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
            user.last_slot = slot
            user.save(update_fields=["last_slot"])
            if not running(user):  # sources were fetched above for everyone; only ranking is left
                run(Refresh.objects.create(user=user, trigger=Refresh.Trigger.SCHEDULED), fetch_first=False)
            refreshed += 1
        if learning.learning_due(user, now):
            try:
                learning.learn(user)
                learned += 1
            except Exception as exc:
                logger.info("Learning skipped for %s: %s", user, exc)
                user.learned_at = now  # try again next week rather than every minute
                user.save(update_fields=["learned_at"])
    Refresh.objects.filter(started_at__lt=now - timedelta(days=14)).delete()
    return {"fetched": fetched, "new": new, "refreshed": refreshed, "learned": learned}

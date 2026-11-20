"""
Fetching feeds: one polite request per source for everyone who follows it.

Requests carry a clear User-Agent and the ETag / Last-Modified the publisher sent last time, so an unchanged
feed answers 304 and costs nothing. Only addresses on the public internet are fetched (people paste
addresses, and the app runs next to other things on the NAS).
"""
import hashlib
import html
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlsplit

import feedparser
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Article, Source

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 100
SNIPPET_LENGTH = 400
# Titles at least this similar (shared significant words) are the same story.
SAME_STORY = 0.6
STORY_WINDOW = timedelta(hours=48)
STOPWORDS = set(
    "a an the and or but of in on at to for from by with as is are was were be been it its this that these those "
    "after before over under into about than then new says say said how why what who will can could would should "
    "has have had not no yes up down out more most".split()
)


class FetchError(Exception):
    pass


def public_url(url):
    """True if the address is http(s) and its host resolves only to public internet addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.is_global
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError):
        # Behind an outgoing HTTP proxy the name may only resolve there; the proxy then decides.
        return bool(urllib.request.getproxies().get(parts.scheme)) and not urllib.request.proxy_bypass(parts.hostname)
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            return False
    return True


class _PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise FetchError("Redirected to an address that is not on the public internet.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_PublicOnlyRedirects)


def get(url, etag="", last_modified="", accept="application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5", timeout=None):
    """(status, body bytes, headers, final url). Status 304 means unchanged; body is then empty."""
    if not public_url(url):
        raise FetchError("Only addresses on the public internet can be fetched.")
    headers = {"User-Agent": settings.FETCH_USER_AGENT, "Accept": accept}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(request, timeout=timeout or settings.FETCH_TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError("The feed is too large.")
            return response.status, body, response.headers, response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", exc.headers, url
        raise FetchError(f"The server answered {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Could not connect: {getattr(exc, 'reason', exc)}") from exc


def clean_text(value, limit):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = " ".join(html.unescape(value).split())
    return value if len(value) <= limit else value[: limit - 1].rsplit(" ", 1)[0] + "…"


def title_words(title):
    words = re.findall(r"\w+", (title or "").lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def fingerprint(title):
    return hashlib.sha1(" ".join(sorted(title_words(title))).encode()).hexdigest()


def similarity(a, b):
    if not a or not b:
        return 0
    return len(a & b) / min(len(a), len(b)) if min(len(a), len(b)) >= 3 else (1.0 if a == b else 0)


def find_story(article):
    """The earlier article from another source about the same story, if any."""
    since = article.published_at - STORY_WINDOW
    candidates = (Article.objects.filter(published_at__gte=since, story__isnull=True)
                  .exclude(source=article.source).exclude(pk=article.pk).only("id", "title", "fingerprint"))
    same = candidates.filter(fingerprint=article.fingerprint).first()
    if same:
        return same
    words = title_words(article.title)
    best, best_score = None, SAME_STORY
    for candidate in candidates.order_by("-published_at")[:500]:
        score = similarity(words, title_words(candidate.title))
        if score >= best_score:
            best, best_score = candidate, score
    return best


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                moment = datetime(*value[:6], tzinfo=dt_timezone.utc)
            except (TypeError, ValueError):
                continue
            return min(moment, timezone.now())
    return timezone.now()


def entry_image(entry):
    for media in entry.get("media_thumbnail", []) + entry.get("media_content", []):
        url = media.get("url", "")
        if url.startswith("http") and media.get("medium", "image") == "image":
            return url
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href", "")
    match = re.search(r'<img[^>]+src="(https?://[^"]+)"', entry.get("summary", "") or "")
    return match.group(1) if match else ""


def parse(body):
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise FetchError("This is not a feed that can be read.")
    return parsed


def store_entries(source, entries):
    """Saves new articles; returns how many were new."""
    new = 0
    cutoff = timezone.now() - timedelta(days=settings.ARTICLE_MAX_AGE_DAYS)
    for entry in entries[:MAX_ITEMS]:
        link = (entry.get("link") or "").strip()
        title = clean_text(entry.get("title"), 500)
        if not link.startswith(("http://", "https://")) or not title:
            continue
        guid = (entry.get("id") or link)[:500]
        if Article.objects.filter(source=source, guid=guid).exists():
            continue
        published = entry_time(entry)
        if published < cutoff:
            continue
        # Aggregators (e.g. Google News topic feeds) name the original publisher per item.
        publisher = clean_text((entry.get("source") or {}).get("title"), 120)
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        snippet = clean_text(entry.get("summary") or entry.get("description"), SNIPPET_LENGTH)
        if snippet.startswith(title[:40]):  # an aggregator's "snippet" that only repeats the headline
            snippet = ""
        article = Article(
            source=source, guid=guid, title=title, link=link[:1000], publisher=publisher,
            snippet=snippet,
            image_url=entry_image(entry)[:1000], published_at=published, fingerprint=fingerprint(title),
        )
        article.story = find_story(article)
        try:
            with transaction.atomic():
                article.save()
        except IntegrityError:
            continue
        new += 1
    return new


def fetch_source(source):
    """Fetches one source and stores its new articles. Returns the number of new articles."""
    source.last_fetched_at = timezone.now()
    try:
        status, body, headers, _ = get(source.feed_url, source.etag, source.last_modified)
        new = 0
        if status != 304:
            parsed = parse(body)
            new = store_entries(source, parsed.entries)
            source.etag = (headers.get("ETag") or "")[:300]
            source.last_modified = (headers.get("Last-Modified") or "")[:100]
        source.error_count = 0
        source.last_error = ""
        source.last_ok_at = source.last_fetched_at
    except FetchError as exc:
        source.error_count += 1
        source.last_error = str(exc)[:300]
        logger.info("Fetching %s failed: %s", source.feed_url, exc)
        new = 0
    source.save(update_fields=["last_fetched_at", "last_ok_at", "error_count", "last_error", "etag", "last_modified"])
    return new


def due(source, now=None):
    """Sources are fetched at most every 15 minutes; one that keeps failing waits longer (up to a day)."""
    now = now or timezone.now()
    if not source.last_fetched_at:
        return True
    wait = timedelta(minutes=15) * (2 ** min(source.error_count, 7))
    return now - source.last_fetched_at >= min(wait, timedelta(days=1))


def fetch_all(sources=None, force=False):
    """Fetches the active, followed sources that are due. Returns (sources fetched, new articles)."""
    if sources is None:
        sources = Source.objects.filter(active=True, followers__active=True).distinct()
    fetched = new = 0
    for source in sources:
        if force or due(source):
            new += fetch_source(source)
            fetched += 1
    return fetched, new


def clean_up():
    """Deletes old articles nobody saved."""
    cutoff = timezone.now() - timedelta(days=settings.ARTICLE_MAX_AGE_DAYS)
    old = Article.objects.filter(published_at__lt=cutoff).exclude(ratings__value="saved")
    return old.delete()[0]

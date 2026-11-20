"""
Finding a publication's feed from what a person types: a feed address, a website, or just a name.

For a name the AI suggests the website (if the person may use AI); for a website the page's
<link rel="alternate"> feeds are read, then the usual feed paths are tried. A publisher without a public
feed is reported as such rather than failing silently.
"""
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from core import ai
from core.models import AIUsage

from .fetch import FetchError, get, parse
from .models import Source

logger = logging.getLogger(__name__)

# Discovery happens while someone waits: shorter timeouts than the background fetch.
TIMEOUT = 8
COMMON_PATHS = ["/feed", "/rss", "/recent.rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/feeds/all.rss", "/rss/feed"]
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml", "text/xml")


@dataclass
class Found:
    query: str
    name: str = ""
    feed_url: str = ""
    site_url: str = ""
    error: str = ""

    @property
    def ok(self):
        return bool(self.feed_url) and not self.error

    def as_dict(self):
        return {"query": self.query, "name": self.name, "feed_url": self.feed_url, "site_url": self.site_url,
                "error": self.error}


class _FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds = []
        self.title = ""
        self.site_name = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "alternate" in (attrs.get("rel") or "").lower():
            if (attrs.get("type") or "").lower() in FEED_TYPES and attrs.get("href"):
                self.feeds.append((attrs["href"], attrs.get("title") or ""))
        elif tag == "meta" and (attrs.get("property") or attrs.get("name") or "").lower() in ("og:site_name", "application-name"):
            self.site_name = self.site_name or " ".join((attrs.get("content") or "").split())
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = " ".join(data.split())


def title_name(title, url):
    """The publication's name from a page title like "World news | The Guardian": the part that matches the
    domain, else the first part."""
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:·]\s+", title or "") if p.strip()]
    if not parts:
        return ""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").split(".")[0]
    for part in parts:
        if host and host in re.sub(r"[^a-z0-9]", "", part.lower()):
            return part[:120]
    return parts[0][:120]


def looks_like_address(text):
    return " " not in text.strip() and "." in text and len(text) > 3


def normalise(address):
    address = address.strip()
    if not re.match(r"^https?://", address, re.I):
        address = "https://" + address
    return address


def site_of(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def try_feed(url):
    """(feed title, final url) if the address is a readable feed with items, else None."""
    try:
        status, body, _, final = get(url, timeout=TIMEOUT)
        parsed = parse(body)
    except FetchError:
        return None
    if status == 304 or not parsed.entries:
        return None
    return (parsed.feed.get("title") or "").strip(), final


def from_address(query, address, name=""):
    url = normalise(address)
    try:
        status, body, headers, final = get(url, timeout=TIMEOUT)
    except FetchError as exc:
        if any(code in str(exc) for code in ("401", "403", "451")):
            return Found(query, name=name, site_url=url,
                         error="This site blocks feed readers, so it is not supported.")
        return Found(query, name=name, site_url=url, error=f"That address could not be opened ({exc}).")
    content_type = (headers.get("Content-Type") or "").lower()
    if "html" not in content_type:
        try:
            parsed = parse(body)
            if parsed.entries:
                title = (parsed.feed.get("title") or "").strip()
                site = parsed.feed.get("link") or site_of(final)
                return Found(query, name=name or title or urlsplit(final).hostname, feed_url=final, site_url=site)
        except FetchError:
            pass
    page = _FeedLinks()
    try:
        page.feed(body.decode("utf-8", errors="replace"))
    except Exception:  # broken html is common; use what was read
        pass
    candidates = [urljoin(final, href) for href, _ in page.feeds]
    candidates += [urljoin(site_of(final), path) for path in COMMON_PATHS]
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        result = try_feed(candidate)
        if result:
            title, feed_url = result
            label = name or page.site_name or title_name(page.title, final) or title or urlsplit(final).hostname
            return Found(query, name=label[:120], feed_url=feed_url, site_url=site_of(final))
    return Found(query, name=name or page.title[:120], site_url=site_of(final),
                 error="No public feed was found for this site, so it is not supported. Try another address.")


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "sites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The name exactly as given."},
                    "name": {"type": "string", "description": "The publication's usual name."},
                    "website": {"type": "string", "description": "Its home page, or \"\" if unknown."},
                    "feed": {"type": "string", "description": "Its main RSS/Atom feed if you know it, else \"\"."},
                },
                "required": ["query", "name", "website", "feed"],
            },
        }
    },
    "required": ["sites"],
}


def suggest_sites(user, names):
    """{name: (proper name, website, feed)} from the AI, or {} if it can't be used."""
    if not names or ai.blocked(user):
        return {}
    try:
        data = ai.ask(
            user, AIUsage.Kind.SOURCES,
            "You help someone add news publications to a feed reader. For each publication name, give its usual "
            "name, its home page and, if you are sure of it, its main RSS or Atom feed address. If a name is "
            "ambiguous, pick the best-known news publication. Unknown: empty strings.",
            "\n".join(names), "save_sites", SUGGEST_SCHEMA, max_output_tokens=3000,
        )
    except ai.AIError:
        return {}
    result = {}
    for site in data.get("sites", []):
        if isinstance(site, dict) and site.get("query"):
            result[str(site["query"]).strip().lower()] = (
                str(site.get("name") or "")[:120], str(site.get("website") or ""), str(site.get("feed") or ""))
    return result


def split_names(text):
    parts = re.split(r"[,;\n]+", text or "")
    seen, names = set(), []
    for part in parts:
        part = " ".join(part.split())
        if part and part.lower() not in seen:
            seen.add(part.lower())
            names.append(part[:200])
    return names[:20]


def discover(user, text):
    """Found entries for everything typed: names, websites or feed addresses, separated by commas or lines."""
    names = split_names(text)
    plain = [n for n in names if not looks_like_address(n)]
    known = {}
    for name in plain:
        source = Source.objects.filter(name__iexact=name, error_count__lt=5).first()
        if source:
            known[name.lower()] = Found(name, name=source.name, feed_url=source.feed_url, site_url=source.site_url)
    suggestions = suggest_sites(user, [n for n in plain if n.lower() not in known])
    results = []
    for name in names:
        if looks_like_address(name):
            results.append(from_address(name, name))
        elif name.lower() in known:
            results.append(known[name.lower()])
        elif name.lower() in suggestions:
            proper, website, feed = suggestions[name.lower()]
            found = None
            if feed:
                result = try_feed(normalise(feed))
                if result:
                    found = Found(name, name=proper or name, feed_url=result[1], site_url=website or site_of(result[1]))
            if not found and website:
                found = from_address(name, website, name=proper or name)
            results.append(found or Found(name, name=proper or name, error="No website was found for this name. "
                                                                            "Paste its address instead."))
        else:
            results.append(Found(name, name=name, error="Paste the website or feed address for this one."))
    return results

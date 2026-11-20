from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import discover, fetch
from .models import Article, Source, UserSource

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Wire News</title><link>https://wire.example/</link>
<item><title>ECB keeps interest rates unchanged in Frankfurt</title><link>https://wire.example/ecb</link><guid>ecb-1</guid>
<description>&lt;p&gt;The central bank &lt;b&gt;held&lt;/b&gt; rates.&lt;/p&gt;</description>
<pubDate>%s</pubDate><enclosure url="https://wire.example/ecb.jpg" type="image/jpeg"/></item>
<item><title>Basel win the cup final</title><link>https://wire.example/basel</link><pubDate>%s</pubDate></item>
</channel></rss>"""

OTHER = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Other</title>
<item><title>Frankfurt: ECB keeps interest rates unchanged again</title><link>https://other.example/ecb</link><pubDate>%s</pubDate></item>
</channel></rss>"""

HTML = b"""<html><head><title>Wire News - Home</title>
<link rel="alternate" type="application/rss+xml" href="/rss.xml"></head><body>Hi</body></html>"""


def rfc822(moment):
    return moment.strftime("%a, %d %b %Y %H:%M:%S +0000").encode()


def rss(body=RSS):
    now = timezone.now()
    return body % ((rfc822(now - timedelta(hours=1)), rfc822(now - timedelta(hours=2)))[: body.count(b"%s")])


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def fake_get(pages):
    """A stand-in for fetch.get serving these {url: (content type, body)}."""
    def get(url, etag="", last_modified="", **kwargs):
        if url not in pages:
            raise fetch.FetchError("The server answered 404.")
        content_type, body = pages[url]
        if body == 304:
            return 304, b"", Headers(), url
        return 200, body, Headers({"Content-Type": content_type, "ETag": '"v1"'}), url
    return get


class FetchTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(name="Wire", feed_url="https://wire.example/rss.xml")

    def fetch(self, source, body):
        with mock.patch.object(fetch, "get", fake_get({source.feed_url: ("application/rss+xml", body)})):
            return fetch.fetch_source(source)

    def test_stores_articles_without_full_text(self):
        self.assertEqual(self.fetch(self.source, rss()), 2)
        ecb = Article.objects.get(guid="ecb-1")
        self.assertEqual(ecb.snippet, "The central bank held rates.")
        self.assertEqual(ecb.image_url, "https://wire.example/ecb.jpg")
        self.assertEqual(Article.objects.get(title__startswith="Basel").guid, "https://wire.example/basel")
        self.source.refresh_from_db()
        self.assertEqual(self.source.etag, '"v1"')
        self.assertEqual(self.fetch(self.source, rss()), 0)  # nothing twice

    def test_same_story_from_another_source_is_merged(self):
        self.fetch(self.source, rss())
        other = Source.objects.create(name="Other", feed_url="https://other.example/feed")
        self.fetch(other, OTHER % rfc822(timezone.now()))
        merged = Article.objects.get(source=other)
        self.assertEqual(merged.story, Article.objects.get(guid="ecb-1"))

    def test_failures_back_off(self):
        with mock.patch.object(fetch, "get", side_effect=fetch.FetchError("boom")):
            fetch.fetch_source(self.source)
        self.source.refresh_from_db()
        self.assertEqual((self.source.error_count, self.source.last_error), (1, "boom"))
        self.assertFalse(fetch.due(self.source, timezone.now() + timedelta(minutes=20)))
        self.assertTrue(fetch.due(self.source, timezone.now() + timedelta(minutes=31)))

    def test_unchanged_feed(self):
        self.assertEqual(self.fetch(self.source, 304), 0)
        self.source.refresh_from_db()
        self.assertEqual(self.source.error_count, 0)

    def test_only_public_addresses(self):
        self.assertFalse(fetch.public_url("http://127.0.0.1/feed"))
        self.assertFalse(fetch.public_url("http://192.168.1.10/feed"))
        self.assertFalse(fetch.public_url("ftp://example.com/feed"))
        with self.assertRaises(fetch.FetchError):
            fetch.get("http://localhost:5000/")

    def test_old_unsaved_articles_are_cleaned_up(self):
        self.fetch(self.source, rss())
        Article.objects.update(published_at=timezone.now() - timedelta(days=60))
        self.assertEqual(fetch.clean_up(), 2)


class DiscoverTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com")

    def test_feed_found_from_website(self):
        pages = {"https://wire.example": ("text/html", HTML), "https://wire.example/rss.xml": ("application/rss+xml", rss())}
        with mock.patch.object(discover, "get", fake_get(pages)):
            found = discover.discover(self.user, "wire.example")
        self.assertEqual((found[0].name, found[0].feed_url), ("Wire News", "https://wire.example/rss.xml"))

    def test_feed_address_directly(self):
        pages = {"https://wire.example/rss.xml": ("application/rss+xml", rss())}
        with mock.patch.object(discover, "get", fake_get(pages)):
            found = discover.discover(self.user, "https://wire.example/rss.xml")
        self.assertTrue(found[0].ok)
        self.assertEqual(found[0].name, "Wire News")

    def test_site_without_feed_is_unsupported(self):
        pages = {"https://nofeed.example": ("text/html", b"<html><title>No</title></html>")}
        with mock.patch.object(discover, "get", fake_get(pages)):
            found = discover.discover(self.user, "nofeed.example")
        self.assertFalse(found[0].ok)
        self.assertIn("not supported", found[0].error)

    @override_settings(OPENAI_API_KEY="")
    def test_name_without_ai_asks_for_address(self):
        found = discover.discover(self.user, "Some Paper")
        self.assertIn("Paste the website", found[0].error)

    def test_name_with_ai(self):
        pages = {"https://wire.example/": ("text/html", HTML), "https://wire.example/rss.xml": ("application/rss+xml", rss())}
        with mock.patch.object(discover, "suggest_sites", return_value={"wire": ("Wire News", "https://wire.example/", "")}), \
                mock.patch.object(discover, "get", fake_get(pages)):
            found = discover.discover(self.user, "Wire")
        self.assertEqual(found[0].feed_url, "https://wire.example/rss.xml")


class SettingsSourceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com", setup_completed=True)
        self.client.force_login(self.user)

    def test_add_confirm_toggle_remove(self):
        pages = {"https://wire.example/rss.xml": ("application/rss+xml", rss())}
        with mock.patch.object(discover, "get", fake_get(pages)):
            page = self.client.post(reverse("sources:add"), {"names": "https://wire.example/rss.xml"})
        self.assertContains(page, "Wire News")
        self.client.post(reverse("sources:add"), {"action": "confirm", "feed": "https://wire.example/rss.xml",
                                                  "name-https://wire.example/rss.xml": "My Wire"})
        link = UserSource.objects.get(user=self.user)
        self.assertEqual(link.display_name, "My Wire")
        self.client.post(reverse("sources:toggle", args=[link.pk]))
        link.refresh_from_db()
        self.assertFalse(link.active)
        other = User.objects.create_user(email="sam@example.com")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("sources:remove", args=[link.pk])).status_code, 404)
        self.client.force_login(self.user)
        self.client.post(reverse("sources:remove", args=[link.pk]))
        self.assertFalse(UserSource.objects.exists())
        self.assertTrue(Source.objects.exists())  # shared sources stay

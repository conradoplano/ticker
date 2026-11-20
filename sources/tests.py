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


GNEWS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>"clearing" - Google News</title>
<item><title>Eurex wins euro swaps business - Financial Times</title><link>https://news.google.com/rss/articles/abc</link>
<description>&lt;a href="x"&gt;Eurex wins euro swaps business&lt;/a&gt;&amp;nbsp;&lt;font&gt;Financial Times&lt;/font&gt;</description>
<pubDate>%s</pubDate><source url="https://www.ft.com">Financial Times</source></item>
</channel></rss>"""


class SuggestTests(TestCase):
    def setUp(self):
        from topics.models import Topic

        self.user = User.objects.create_user(email="pat@example.com", setup_completed=True)
        Topic.objects.create(user=self.user, name="Clearing and derivatives")

    def test_topic_feed_url(self):
        from .suggest import topic_feed_url

        self.assertEqual(topic_feed_url('"clearing house"', "de", "de"),
                         "https://news.google.com/rss/search?q=%22clearing+house%22&hl=de&gl=DE&ceid=DE%3Ade")
        self.assertIn("hl=en-US&gl=US&ceid=US%3Aen", topic_feed_url("AI"))

    def test_aggregator_items_name_their_publisher(self):
        source = Source.objects.create(name="Topic: Clearing", feed_url="https://news.google.com/rss/search?q=clearing")
        with mock.patch.object(fetch, "get", fake_get({source.feed_url: ("application/rss+xml", GNEWS % rfc822(timezone.now()))})):
            fetch.fetch_source(source)
        article = Article.objects.get()
        self.assertEqual((article.title, article.publisher, article.snippet), ("Eurex wins euro swaps business", "Financial Times", ""))
        self.assertEqual(article.source_name, "Financial Times")

    @override_settings(OPENAI_API_KEY="")
    def test_without_ai_only_topic_feeds(self):
        from .suggest import suggest

        results = suggest(self.user, ["Clearing and derivatives"])
        self.assertEqual([(r["kind"], r["name"]) for r in results], [("topic", "Topic: Clearing and derivatives")])

    @override_settings(OPENAI_API_KEY="test-key")
    def test_ai_publications_are_checked(self):
        from . import suggest as module

        self.user.ai_approved = True
        self.user.save()
        answer = {"publications": [
            {"name": "Wire News", "website": "https://wire.example/", "feed": "", "topics": ["Clearing and derivatives"], "why": "Market structure"},
            {"name": "Dead Site", "website": "https://dead.example/", "feed": "", "topics": [], "why": ""},
            {"name": "Direct", "website": "", "feed": "https://direct.example/rss", "topics": [], "why": ""},
        ], "topic_feeds": [{"topic": "clearing and derivatives", "query": '"derivatives clearing"', "language": "en", "country": "GB"}]}
        pages = {"https://wire.example/": ("text/html", HTML), "https://wire.example/rss.xml": ("application/rss+xml", rss()),
                 "https://direct.example/rss": ("application/rss+xml", rss())}
        with mock.patch.object(module, "ask", return_value=answer), mock.patch.object(discover, "get", fake_get(pages)):
            results = module.suggest(self.user, ["Clearing and derivatives"])
        names = [(r["kind"], r["name"]) for r in results]
        self.assertIn(("publication", "Wire News"), names)
        self.assertIn(("publication", "Direct"), names)
        self.assertNotIn("Dead Site", [n for _, n in names])
        topic = [r for r in results if r["kind"] == "topic"][0]
        self.assertIn("gl=GB", topic["feed_url"])

    @override_settings(OPENAI_API_KEY="")
    def test_settings_page_and_following_a_topic_feed(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("sources:suggest")), "Clearing and derivatives")
        page = self.client.get(reverse("sources:suggest_results"))
        self.assertContains(page, "Topic: Clearing and derivatives")
        url = page.context["found"][0]["feed_url"]
        self.client.post(reverse("sources:add"), {"action": "confirm", "feed": url})
        link = UserSource.objects.get(user=self.user)
        self.assertEqual(link.source.topic_query, "Clearing and derivatives")

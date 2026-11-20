import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core import ai
from core.models import AIUsage
from sources.models import Article, Source, UserSource
from topics.models import Mute, Topic

from . import build, learning, refresh, scoring
from .models import ArticleScore, Rating, Refresh


def ai_response(name, data, tokens=(1000, 200)):
    call = SimpleNamespace(type="function_call", name=name, arguments=json.dumps(data))
    return SimpleNamespace(output=[call], usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
                           model="gpt-5.4-mini")


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com", name="Pat", setup_completed=True)
        self.source = Source.objects.create(name="Wire", feed_url="https://wire.example/feed")
        UserSource.objects.create(user=self.user, source=self.source)
        Topic.objects.create(user=self.user, name="Markets and central banks")

    def article(self, title, hours=1, source=None, **extra):
        return Article.objects.create(source=source or self.source, guid=title, title=title,
                                      link=f"https://wire.example/{abs(hash(title))}", fingerprint=title,
                                      published_at=timezone.now() - timedelta(hours=hours), **extra)

    def use_ai(self, *responses):
        self.user.ai_approved = True
        self.user.save()
        client = SimpleNamespace(responses=SimpleNamespace(create=mock.Mock(side_effect=list(responses))))
        patcher = mock.patch.object(ai, "get_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client.responses.create


@override_settings(OPENAI_API_KEY="")
class KeywordScoringTests(Base):
    def test_ranks_by_topic_words_and_drops_muted(self):
        bank = self.article("Central banks surprise markets")
        cat = self.article("Cat rescued from tree")
        star = self.article("Celebrity wedding in Rome")
        Mute.objects.create(user=self.user, subject="celebrity")
        count, method = scoring.score_new(self.user)
        self.assertEqual((count, method), (3, None) if False else (3, ArticleScore.Method.KEYWORDS))
        scores = {s.article: s for s in ArticleScore.objects.filter(user=self.user)}
        self.assertGreater(scores[bank].score, scores[cat].score)
        self.assertEqual(scores[bank].topic, "Markets and central banks")
        self.assertTrue(scores[star].hidden)
        self.assertEqual(scoring.score_new(self.user), (0, None))  # nothing twice

    def test_one_article_per_story(self):
        lead = self.article("Central banks raise rates")
        other = Source.objects.create(name="Other", feed_url="https://other.example/feed")
        UserSource.objects.create(user=self.user, source=other)
        self.article("Central banks raise rates again", source=other, story=lead)
        scoring.score_new(self.user)
        self.assertEqual(ArticleScore.objects.count(), 1)


@override_settings(OPENAI_API_KEY="test-key")
class AIScoringTests(Base):
    def test_ai_scores_topics_and_discovery(self):
        bank = self.article("ECB holds rates")
        chess = self.article("Chess final goes to tiebreaks", hours=2)
        create = self.use_ai(ai_response("save_scores", {"items": [
            {"n": 1, "score": 88, "topic": "markets AND central banks", "new_topic": "", "reason": "You follow Markets",
             "summary": "The ECB kept its rates unchanged."},
            {"n": 2, "score": 55, "topic": "", "new_topic": "Chess", "reason": "Something new"},
        ]}))
        self.assertEqual(scoring.score_new(self.user), (2, ArticleScore.Method.AI))
        bank_score = ArticleScore.objects.get(article=bank)
        self.assertEqual((bank_score.score, bank_score.topic), (88, "Markets and central banks"))
        self.assertTrue(ArticleScore.objects.get(article=chess).is_discovery)
        prompt = create.call_args_list[0].kwargs["input"][0]["content"]
        self.assertIn("Followed topics: Markets and central banks", prompt)
        self.assertEqual(AIUsage.objects.get().kind, "score")
        bank.refresh_from_db()
        self.assertEqual(bank.summary, "The ECB kept its rates unchanged.")

    def test_summary_is_written_once_and_shared(self):
        bank = self.article("EZB lässt Zinsen unverändert", summary="The ECB kept its rates unchanged.")
        other = User.objects.create_user(email="sam@example.com", setup_completed=True)
        UserSource.objects.create(user=other, source=self.source)
        self.user = other
        create = self.use_ai(ai_response("save_scores", {"items": [
            {"n": 1, "score": 70, "topic": "", "new_topic": "", "reason": "", "summary": "Something else."}]}))
        scoring.score_new(other)
        self.assertIn("Summary: The ECB kept its rates unchanged.", create.call_args_list[0].kwargs["input"][0]["content"])
        bank.refresh_from_db()
        self.assertEqual(bank.summary, "The ECB kept its rates unchanged.")

    def test_falls_back_to_keywords_when_ai_fails(self):
        self.article("Central banks meet")
        self.use_ai(Exception("down"))
        self.assertEqual(scoring.score_new(self.user)[1], ArticleScore.Method.KEYWORDS)

    def test_over_daily_limit_uses_keywords(self):
        self.article("Central banks meet")
        self.use_ai()
        AIUsage.objects.create(user=self.user, kind="score", cost="5")
        self.assertEqual(scoring.score_new(self.user)[1], ArticleScore.Method.KEYWORDS)

    def test_learned_profile(self):
        a = self.article("ECB holds rates")
        for i in range(5):
            Rating.objects.create(user=self.user, article=self.article(f"Story {i}"), value="up")
        Rating.objects.create(user=self.user, article=a, value="down")
        self.assertTrue(learning.learning_due(self.user))
        self.use_ai(ai_response("save_profile", {"profile": "You like monetary policy."}))
        learning.learn(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.learned_profile, "You like monetary policy.")
        self.assertFalse(learning.learning_due(self.user))

    def test_briefing(self):
        a = self.article("ECB holds rates")
        ArticleScore.objects.create(user=self.user, article=a, score=90)
        self.use_ai(ai_response("save_briefing", {"items": [{"number": 1, "sentence": "The ECB paused."}]}))
        items = learning.build_briefing(self.user)
        self.assertEqual(items[0]["sentence"], "The ECB paused.")


class OneLineTests(TestCase):
    def test_summary_else_first_sentence_of_snippet(self):
        self.assertEqual(Article(summary="AI line.", snippet="Long text.").one_line, "AI line.")
        snippet = "The central bank held its deposit rate at two percent. Analysts had expected this. More follows."
        self.assertEqual(Article(snippet=snippet).one_line, "The central bank held its deposit rate at two percent.")
        self.assertEqual(Article(snippet="").one_line, "")


@override_settings(OPENAI_API_KEY="test-key")
class StoryTests(Base):
    def test_ai_labels_stories_and_reuses_current_ones(self):
        old = self.article("Tashima challenges Infantino", hours=3)
        ArticleScore.objects.create(user=self.user, article=old, score=70, story_key="FIFA presidential race")
        a = self.article("Diallo enters FIFA race")
        b = self.article("ECB holds rates", hours=2)
        create = self.use_ai(ai_response("save_scores", {"items": [
            {"n": 1, "score": 70, "topic": "", "new_topic": "", "reason": "", "summary": "", "story": "fifa PRESIDENTIAL race"},
            {"n": 2, "score": 80, "topic": "", "new_topic": "", "reason": "", "summary": "", "story": "ECB rate decision"}]}))
        scoring.score_new(self.user)
        self.assertIn('- FIFA presidential race (e.g. "Tashima challenges Infantino")',
                      create.call_args_list[0].kwargs["input"][0]["content"])
        self.assertEqual(ArticleScore.objects.get(article=a).story_key, "FIFA presidential race")
        self.assertEqual(ArticleScore.objects.get(article=b).story_key, "ECB rate decision")

    def test_labels_of_the_same_story_are_merged(self):
        for title, label in [("Tashima stays in race", "Tashima FIFA race"), ("Tashima vows clean FIFA", "Tashima FIFA campaign"),
                             ("Infantino faces two rivals", "Infantino reelection challenge"), ("ECB holds rates", "ECB rate decision")]:
            ArticleScore.objects.create(user=self.user, article=self.article(title), score=70, story_key=label)
        create = self.use_ai(ai_response("save_groups", {"groups": [
            {"labels": [1, 2, 3], "name": "FIFA presidential election"}, {"labels": [4], "name": "ECB"}]}))
        self.assertEqual(scoring.merge_stories(self.user), 3)
        self.assertIn("Tashima FIFA race", create.call_args.kwargs["input"][0]["content"])
        self.assertEqual(sorted(set(ArticleScore.objects.values_list("story_key", flat=True))),
                         ["ECB rate decision", "FIFA presidential election"])

    def test_unlabelled_items_get_labels(self):
        a = self.article("Tashima challenges Infantino")
        ArticleScore.objects.create(user=self.user, article=a, score=70)
        self.use_ai(ai_response("save_stories", {"items": [{"n": 1, "story": "FIFA presidential race"}]}))
        self.assertEqual(scoring.label_stories(self.user), 1)
        self.assertEqual(ArticleScore.objects.get(article=a).story_key, "FIFA presidential race")


class StoryFeedTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.fifa = []
        for i, (title, publisher) in enumerate([("Tashima challenges Infantino", "ESPN"), ("Diallo enters FIFA race", "France 24"),
                                                 ("Infantino faces two rivals", "NDTV"), ("FIFA race heats up", "Sportstar"),
                                                 ("Quiet reformer takes on Infantino", "ABC"), ("Tashima stays in race", "Gleaner")]):
            article = self.article(title, hours=1 + i, publisher=publisher)
            self.fifa.append(ArticleScore.objects.create(user=self.user, article=article, score=80 - i,
                                                         story_key="FIFA presidential race"))
        self.ecb = ArticleScore.objects.create(user=self.user, article=self.article("ECB holds rates"), score=60,
                                               story_key="ECB rate decision")

    def test_one_card_per_story(self):
        rows = build.items(self.user)
        self.assertEqual([s.article.title for s in rows], ["Tashima challenges Infantino", "ECB holds rates"])
        page = self.client.get(reverse("feed:home"))
        self.assertContains(page, " · also in ")
        self.assertContains(page, "France 24")
        self.assertContains(page, "and 1 more")
        self.assertContains(page, '<span class="story-name">FIFA presidential race</span>')
        self.assertContains(page, '<span class="story-count">6</span>')
        self.assertNotContains(page, '<span class="story-name">ECB rate decision</span>')  # one article: no link
        self.assertEqual(len(build.items(self.user, mode="latest")), 2)

    def test_story_keeps_the_topic_you_came_from(self):
        Topic.objects.create(user=self.user, name="FIFA")
        ArticleScore.objects.filter(story_key="FIFA presidential race").update(topic="FIFA")
        ArticleScore.objects.filter(article__title="FIFA race heats up").update(topic="Football")  # another topic
        page = self.client.get(reverse("feed:home") + "?topic=FIFA")
        self.assertContains(page, 'href="/?topic=FIFA&amp;story=FIFA+presidential+race"')
        story = self.client.get(reverse("feed:home") + "?topic=FIFA&story=FIFA+presidential+race")
        self.assertEqual(len(story.context["scores"]), 6)  # the whole story, not only the topic's part
        self.assertContains(story, '<a href="/?topic=FIFA" class="small">‹ Back to FIFA</a>')
        card = self.client.post(reverse("feed:rate", args=[self.fifa[0].article_id]), {"value": "up"},
                                HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL="http://testserver/?topic=FIFA")
        self.assertContains(card, 'href="/?topic=FIFA&amp;story=FIFA+presidential+race"')

    def test_mark_story_unread_from_any_of_its_cards(self):
        url = reverse("feed:rate", args=[self.fifa[0].article_id])
        self.client.get(reverse("feed:open", args=[self.fifa[3].article_id]))  # read through another article
        card = self.client.post(url, {"value": "read"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(card, "item read")
        self.assertFalse(Rating.objects.filter(user=self.user, value__in=Rating.READ_VALUES).exists())
        self.assertEqual(len(build.items(self.user)), 2)  # the story is back
        card = self.client.post(url, {"value": "read"}, HTTP_HX_REQUEST="true")
        self.assertContains(card, "item read")
        self.assertEqual([s.article.title for s in build.items(self.user)], ["ECB holds rates"])

    def test_story_view(self):
        page = self.client.get(reverse("feed:home") + "?story=FIFA%20presidential%20race")
        self.assertEqual(len(page.context["scores"]), 6)
        self.assertContains(page, "Every article on this story")

    def test_reading_one_article_hides_the_story_until_there_is_news(self):
        self.client.get(reverse("feed:open", args=[self.fifa[2].article_id]))
        self.assertEqual([s.article.title for s in build.items(self.user)], ["ECB holds rates"])
        self.assertContains(self.client.get(reverse("feed:home")), "1 read story hidden")
        later = ArticleScore.objects.create(user=self.user, article=self.article("Infantino responds", hours=0),
                                            score=75, story_key="FIFA presidential race")
        self.assertEqual([s.article.title for s in build.items(self.user)][0], "Infantino responds")
        self.assertIn(later.pk, [s.pk for s in build.items(self.user)])


class FeedBuildTests(Base):
    def test_discovery_is_mixed_in(self):
        for i in range(18):
            ArticleScore.objects.create(user=self.user, article=self.article(f"Regular {i}", hours=i + 1), score=80,
                                        topic="Markets and central banks")
        for i in range(3):
            ArticleScore.objects.create(user=self.user, article=self.article(f"Discovery {i}", hours=1), score=60,
                                        new_topic="Chess")
        items = build.items(self.user)
        positions = [i for i, s in enumerate(items) if s.is_discovery]
        self.assertEqual(positions, [9, 19])  # one in ten
        self.user.discovery_share = 0
        self.assertFalse(any(s.is_discovery for s in build.items(self.user)))

    def test_latest_and_filters(self):
        old = self.article("Old news", hours=10)
        new = self.article("New news", hours=1)
        ArticleScore.objects.create(user=self.user, article=old, score=90)
        ArticleScore.objects.create(user=self.user, article=new, score=20)
        self.assertEqual([s.article for s in build.items(self.user, mode="latest")], [new, old])
        self.assertEqual([s.article for s in build.items(self.user)], [old, new])
        self.assertEqual([s.article for s in build.items(self.user, q="new news")], [new])


class FeedViewTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.a = self.article("ECB holds rates", summary="The ECB kept rates on hold to watch inflation.")
        self.score = ArticleScore.objects.create(user=self.user, article=self.a, score=80, topic="Markets and central banks",
                                                 reason="You follow Markets")

    def test_setup_replaces_feed_until_done(self):
        self.user.setup_completed = False
        self.user.save()
        self.assertRedirects(self.client.get(reverse("feed:home")), reverse("feed:setup"))

    @mock.patch.object(refresh, "refresh_in_background")
    def test_feed_shows_items_and_refreshes_after_a_while(self, background):
        self.article("Unscored story")
        page = self.client.get(reverse("feed:home"))
        self.assertContains(page, "ECB holds rates")
        self.assertContains(page, "The ECB kept rates on hold to watch inflation.")
        background.assert_called_once()
        self.client.get(reverse("feed:home"))
        background.assert_called_once()  # not again within the hour

    def test_open_counts_as_interest(self):
        response = self.client.get(reverse("feed:open", args=[self.a.id]))
        self.assertEqual(response["Location"], self.a.link)
        self.assertTrue(Rating.objects.filter(user=self.user, article=self.a, value="opened").exists())

    def test_opened_articles_are_shown_as_read(self):
        dup_source = Source.objects.create(name="Other", feed_url="https://other.example/feed")
        dup = self.article("EZB hält die Zinsen", source=dup_source, story=self.a)
        page = self.client.get(reverse("feed:home"))
        self.assertNotContains(page, "item read")
        self.client.get(reverse("feed:open", args=[dup.id]))  # opened from "Also in"
        self.assertNotContains(self.client.get(reverse("feed:home")), "ECB holds rates")
        page = self.client.get(reverse("feed:home") + "?read=1")
        self.assertContains(page, 'class="card item read"')
        self.assertContains(page, ">Read<")
        self.client.post(reverse("feed:rate", args=[self.a.id]), {"value": "saved"})
        self.client.get(reverse("feed:open", args=[self.a.id]))
        self.assertContains(self.client.get(reverse("feed:saved")), "item read")

    def test_mark_as_read_and_unread(self):
        url = reverse("feed:rate", args=[self.a.id])
        card = self.client.post(url, {"value": "read"}, HTTP_HX_REQUEST="true")
        self.assertContains(card, 'class="card item read"')
        self.assertContains(card, "Mark as unread")
        self.assertFalse(learning.learning_due(self.user))  # not a sign of interest
        self.client.get(reverse("feed:open", args=[self.a.id]))
        card = self.client.post(url, {"value": "read"}, HTTP_HX_REQUEST="true")  # unread clears opened too
        self.assertContains(card, "Mark as read")
        self.assertNotContains(card, "item read")
        self.assertFalse(Rating.objects.filter(user=self.user, value__in=Rating.READ_VALUES).exists())
        # a duplicate opened elsewhere makes the story read; unread clears it as well
        other = Source.objects.create(name="Other", feed_url="https://other.example/feed")
        dup = self.article("EZB hält die Zinsen", source=other, story=self.a)
        self.client.get(reverse("feed:open", args=[dup.id]))
        self.assertContains(self.client.post(url, {"value": "read"}, HTTP_HX_REQUEST="true"), "Mark as read")

    def test_read_articles_are_hidden_on_reload(self):
        other = self.article("Bund yields fall")
        ArticleScore.objects.create(user=self.user, article=other, score=70, topic="Markets and central banks")
        self.client.get(reverse("feed:open", args=[self.a.id]))
        page = self.client.get(reverse("feed:home"))
        self.assertNotContains(page, "ECB holds rates")
        self.assertContains(page, "Bund yields fall")
        self.assertContains(page, "1 read story hidden")
        page = self.client.get(reverse("feed:home") + "?read=1")
        self.assertContains(page, "ECB holds rates")
        self.assertContains(page, "Hide read")

    def test_topic_view(self):
        chess = self.article("Chess final")
        ArticleScore.objects.create(user=self.user, article=chess, score=60, new_topic="Chess")
        page = self.client.get(reverse("feed:home"))
        self.assertContains(page, 'href="/?topic=Markets%20and%20central%20banks"')
        page = self.client.get(reverse("feed:home") + "?topic=Chess")
        self.assertContains(page, "Chess final")
        self.assertNotContains(page, "ECB holds rates")
        self.assertContains(page, "+ Follow this topic")
        response = self.client.post(reverse("topics:add"), {"name": "Chess", "next": "/?topic=Chess"})
        self.assertRedirects(response, "/?topic=Chess", fetch_redirect_response=False)
        self.assertNotContains(self.client.get(reverse("feed:home") + "?topic=Chess"), "You don't follow this topic")

    def test_next_pages_load_on_scroll_and_stay_stable(self):
        for i in range(30):
            ArticleScore.objects.create(user=self.user, article=self.article(f"Story {i}", hours=1 + i / 10), score=50)
        page = self.client.get(reverse("feed:home"))
        self.assertContains(page, 'hx-trigger="intersect once"')
        asof = page.context["asof"]
        first = [s.article_id for s in page.context["scores"]]
        self.client.get(reverse("feed:open", args=[first[0]]))  # read while scrolling: later pages don't shift
        more = self.client.get(reverse("feed:home") + f"?page=2&asof={asof}", HTTP_HX_REQUEST="true")
        second = [s.article_id for s in more.context["scores"]]
        self.assertEqual(len(first) + len(second), 31)
        self.assertFalse(set(first) & set(second))

    def test_thumbs(self):
        url = reverse("feed:rate", args=[self.a.id])
        self.assertContains(self.client.post(url, {"value": "up"}, HTTP_HX_REQUEST="true"), "aria-pressed=\"true\"")
        response = self.client.post(url, {"value": "down"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.content, b"")
        self.assertEqual(list(Rating.objects.values_list("value", flat=True)), ["down"])
        self.assertNotContains(self.client.get(reverse("feed:home")), "ECB holds rates")

    def test_save_for_later(self):
        self.client.post(reverse("feed:rate", args=[self.a.id]), {"value": "saved"})
        self.assertContains(self.client.get(reverse("feed:saved")), "ECB holds rates")

    def test_follow_and_mute_topic(self):
        chess = self.article("Chess final")
        ArticleScore.objects.create(user=self.user, article=chess, score=60, new_topic="Chess")
        self.client.post(reverse("feed:follow_topic", args=[chess.id]))
        self.assertTrue(Topic.objects.filter(user=self.user, name="Chess", active=True).exists())
        self.assertEqual(ArticleScore.objects.get(article=chess).topic, "Chess")
        self.client.post(reverse("feed:mute_topic", args=[chess.id]))
        self.assertTrue(Mute.objects.filter(user=self.user, subject="Chess").exists())
        self.assertFalse(Topic.objects.get(user=self.user, name="Chess").active)
        self.assertTrue(ArticleScore.objects.get(article=chess).hidden)

    def test_cannot_touch_other_peoples_items(self):
        other = User.objects.create_user(email="sam@example.com", setup_completed=True)
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("feed:follow_topic", args=[self.a.id])).status_code, 404)

    @override_settings(OPENAI_API_KEY="")
    def test_briefing_block(self):
        page = self.client.get(reverse("feed:briefing"))
        self.assertContains(page, "ECB holds rates")
        self.assertContains(page, " open")

    def test_settings_export_and_delete(self):
        page = self.client.get(reverse("feed:settings"))
        self.assertContains(page, "Markets and central banks")
        self.client.post(reverse("feed:settings"), {"form": "prefs", "time_zone": "Europe/Zurich",
                                                    "refresh_times": "17:30, 7:00", "discovery_share": "20"})
        self.user.refresh_from_db()
        self.assertEqual((self.user.refresh_times, self.user.discovery_share), (["07:00", "17:30"], 20))
        data = json.loads(self.client.get(reverse("feed:export")).content)
        self.assertEqual(data["topics"][0]["name"], "Markets and central banks")
        self.client.post(reverse("feed:delete"), {"email": "wrong@example.com"})
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.client.post(reverse("feed:delete"), {"email": "PAT@example.com"})
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())


class ScheduleTests(Base):
    def test_current_slot(self):
        self.user.time_zone = "Europe/Zurich"
        self.user.refresh_times = ["07:00", "12:00"]
        zurich = ZoneInfo("Europe/Zurich")
        at = lambda h, m: datetime(2026, 11, 20, h, m, tzinfo=zurich)
        self.assertIsNone(refresh.current_slot(self.user, at(6, 59)))
        self.assertEqual(refresh.current_slot(self.user, at(7, 1)), "2026-11-20 07:00")
        self.assertIsNone(refresh.current_slot(self.user, at(10, 30)))  # missed by more than 3 hours
        self.assertEqual(refresh.current_slot(self.user, at(12, 5)), "2026-11-20 12:00")

    @override_settings(OPENAI_API_KEY="")
    @mock.patch("sources.fetch.fetch_all", return_value=(0, 0))
    def test_run_due_scores_once_per_slot(self, fetch_all):
        self.article("Central banks meet")
        self.user.refresh_times = [timezone.now().astimezone(self.user.tz).strftime("%H:%M")]
        self.user.save()
        self.assertEqual(refresh.run_due()["refreshed"], 1)
        self.assertEqual(ArticleScore.objects.count(), 1)
        self.assertEqual(refresh.run_due()["refreshed"], 0)


@override_settings(OPENAI_API_KEY="")
class SetupWizardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="new@example.com")
        self.client.force_login(self.user)

    def test_full_wizard_without_ai(self):
        self.client.post(reverse("feed:setup"), {"description": "I follow football and markets"})
        page = self.client.get(reverse("feed:setup_topics"))
        self.assertContains(page, 'value="Football" checked')
        self.assertContains(self.client.post(reverse("feed:setup_topics"), {}), "Pick at least one topic")
        self.client.post(reverse("feed:setup_topics"), {"topic": ["Football"], "own": "FC Basel, clearing and derivatives"})
        from sources.discover import Found
        with mock.patch("feed.setup.discover", return_value=[
                Found("Wire", name="Wire", feed_url="https://wire.example/feed", site_url="https://wire.example/"),
                Found("Nope", name="Nope", error="No public feed was found.")]):
            page = self.client.post(reverse("feed:setup_sources"), {"action": "find", "names": "Wire, Nope"})
        self.assertContains(page, "No public feed was found.")
        self.client.post(reverse("feed:setup_sources"), {"feed": ["https://wire.example/feed"]})
        self.client.post(reverse("feed:setup_preferences"), {"time_zone": "Europe/Berlin", "frequency": "custom",
                                                             "times": "08:00, 18:00", "discovery": "20", "avoid": "no gossip"})
        self.assertContains(self.client.get(reverse("feed:setup_build")), "3 topics, 1 source")
        with mock.patch("sources.fetch.fetch_all") as fetch_all:
            response = self.client.post(reverse("feed:setup_build_run"), HTTP_HX_REQUEST="true")
        fetch_all.assert_called_once()
        self.assertEqual(response["HX-Redirect"], reverse("feed:home"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.setup_completed)
        self.assertEqual((self.user.refresh_times, self.user.discovery_share, self.user.avoid_text),
                         (["08:00", "18:00"], 20, "no gossip"))
        self.assertEqual(sorted(self.user.topics.values_list("name", flat=True)),
                         ["FC Basel", "Football", "clearing and derivatives"])
        self.assertEqual(UserSource.objects.get(user=self.user).display_name, "Wire")

    @override_settings(OPENAI_API_KEY="test-key")
    def test_description_with_ai_suggests(self):
        self.user.ai_approved = True
        self.user.save()
        client = SimpleNamespace(responses=SimpleNamespace(create=mock.Mock(return_value=ai_response(
            "save_suggestions", {"topics": ["AI", "Swiss politics"], "sources": ["NZZ"]}))))
        with mock.patch.object(ai, "get_client", return_value=client):
            self.client.post(reverse("feed:setup"), {"description": "Swiss tech worker"})
        self.assertContains(self.client.get(reverse("feed:setup_topics")), 'value="Swiss politics" checked')
        self.assertContains(self.client.get(reverse("feed:setup_sources")), 'value="NZZ"')

    def test_sources_step_suggests_for_the_topics(self):
        self.client.post(reverse("feed:setup_topics"), {"topic": ["Football"]})
        self.assertContains(self.client.get(reverse("feed:setup_sources")), "Finding sources for your topics")
        part = self.client.get(reverse("feed:setup_sources_suggest"))
        self.assertContains(part, "Topic: Football")
        self.assertNotContains(self.client.get(reverse("feed:setup_sources")), "Finding sources for your topics")
        url = part.context["found"][0]["feed_url"]
        self.client.post(reverse("feed:setup_sources"), {"feed": [url]})
        self.client.post(reverse("feed:setup_preferences"), {"time_zone": "Europe/Berlin", "frequency": "4", "discovery": "10"})
        with mock.patch("sources.fetch.fetch_all"):
            self.client.post(reverse("feed:setup_build_run"))
        self.assertEqual(UserSource.objects.get(user=self.user).source.topic_query, "Football")

    def test_skip_uses_defaults(self):
        self.assertRedirects(self.client.post(reverse("feed:setup_skip")), reverse("feed:settings"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.setup_completed)
        self.assertEqual((len(self.user.refresh_times), self.user.discovery_share), (4, 10))


class AdminPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", is_staff=True, setup_completed=True)
        self.pat = User.objects.create_user(email="pat@example.com", name="Pat", setup_completed=True)
        AIUsage.objects.create(user=self.pat, kind="score", model="gpt-5.4-mini", input_tokens=10, cost="0.0300")
        AIUsage.objects.create(user=self.pat, kind="briefing", model="odd-model", input_tokens=10, cost="0")

    def test_staff_only(self):
        self.client.force_login(self.pat)
        self.assertEqual(self.client.get(reverse("feed:manage")).status_code, 403)
        self.assertEqual(self.client.post(reverse("feed:manage_person", args=[self.pat.pk]), {"action": "approve"}).status_code, 403)

    def test_costs_waiting_and_actions(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("feed:manage"))
        self.assertContains(page, "Waiting for AI (1)")
        self.assertContains(page, "Today $0.030 of $0.10")
        self.assertContains(page, "Scoring")
        self.assertContains(page, "“odd-model” has no price")
        url = reverse("feed:manage_person", args=[self.pat.pk])
        self.client.post(url, {"action": "approve"})
        self.client.post(url, {"action": "limit", "limit": "0.5"})
        self.pat.refresh_from_db()
        self.assertTrue(self.pat.ai_approved)
        self.assertEqual(str(self.pat.ai_daily_limit), "0.50")
        self.client.post(url, {"action": "limit", "limit": "-3"})
        self.client.post(url, {"action": "limit", "limit": ""})
        self.client.post(url, {"action": "suspend"})
        self.pat.refresh_from_db()
        self.assertIsNone(self.pat.ai_daily_limit)
        self.assertFalse(self.pat.is_active)
        self.client.post(reverse("feed:manage_person", args=[self.admin.pk]), {"action": "suspend"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)


@override_settings(OPENAI_API_KEY="test-key", AI_TRIAL_HOURS=24, AI_TRIAL_LIMIT_USD=Decimal("0.10"),
                   AI_TRIAL_GLOBAL_DAILY_LIMIT_USD=Decimal("0.50"), AI_DAILY_LIMIT_USD=Decimal("0.10"))
class AITrialTests(TestCase):
    def setUp(self):
        self.new = User.objects.create_user(email="new@example.com")

    def test_new_people_get_a_day_of_ai(self):
        self.assertTrue(ai.on_trial(self.new))
        self.assertEqual(ai.blocked(self.new), "")
        User.objects.filter(pk=self.new.pk).update(date_joined=timezone.now() - timedelta(hours=25))
        self.new.refresh_from_db()
        self.assertIn("free AI day has ended", ai.blocked(self.new))
        self.new.ai_approved = True
        self.assertEqual(ai.blocked(self.new), "")

    def test_trial_is_capped_per_person_for_the_whole_trial(self):
        User.objects.filter(pk=self.new.pk).update(date_joined=timezone.now() - timedelta(hours=23))
        self.new.refresh_from_db()
        AIUsage.objects.create(user=self.new, kind="setup", cost="0.06", created_at=timezone.now() - timedelta(hours=22))
        AIUsage.objects.create(user=self.new, kind="score", cost="0.05")
        self.assertIn("used the free AI", ai.blocked(self.new))

    def test_all_trials_together_are_capped_per_day(self):
        for i in range(6):
            other = User.objects.create_user(email=f"bot{i}@example.com")
            AIUsage.objects.create(user=other, kind="setup", cost="0.09")
        self.assertIn("used up for today", ai.blocked(self.new))
        approved = User.objects.create_user(email="ok@example.com", ai_approved=True)
        self.assertEqual(ai.blocked(approved), "")  # approved people don't depend on the trial budget

    def test_admin_switching_off_also_ends_the_trial(self):
        self.new.ai_daily_limit = Decimal("0")
        self.assertIn("switched off", ai.blocked(self.new))

    @override_settings(AI_TRIAL_HOURS=0)
    def test_no_trial(self):
        self.assertIn("free AI day has ended", ai.blocked(self.new))

    def test_admin_page_shows_the_trial(self):
        admin = User.objects.create_user(email="admin@example.com", is_staff=True)
        self.client.force_login(admin)
        page = self.client.get(reverse("feed:manage"))
        self.assertContains(page, "AI trial")
        self.assertContains(page, "Free AI until")
        self.assertContains(page, "New accounts on trial, today")


@override_settings(OPENAI_API_KEY="")
class RefreshJobTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_refresh_now_starts_a_job_and_shows_its_progress(self):
        with mock.patch.object(refresh.threading, "Thread") as thread:
            banner = self.client.post(reverse("feed:refresh"), HTTP_HX_REQUEST="true")
            again = self.client.post(reverse("feed:refresh"), HTTP_HX_REQUEST="true")
        thread.return_value.start.assert_called_once()  # one job at a time
        job = Refresh.objects.get()
        self.assertEqual(job.trigger, "manual")
        self.assertContains(banner, "Checking your sources")
        self.assertContains(banner, 'hx-trigger="every 2s"')
        self.assertEqual(again.context["job"], job)
        self.assertContains(self.client.get(reverse("feed:home")), "Checking your sources")  # also after a reload

    def test_run_records_progress_and_what_it_added(self):
        def fetched(source):
            self.article("Central banks raise rates")
            self.article("Cat rescued from tree", hours=2)
            return 2
        job = Refresh.objects.create(user=self.user)
        with mock.patch("sources.fetch.fetch_source", side_effect=fetched):
            refresh.run(job)
        job.refresh_from_db()
        self.assertEqual((job.status, job.sources_total, job.sources_checked, job.articles_fetched, job.ranked),
                         ("done", 1, 1, 2, 2))
        self.assertEqual((job.new_items, job.new_stories), (2, 2))
        banner = self.client.get(reverse("feed:refresh_status", args=[job.pk]))
        self.assertContains(banner, "2 new stories")
        self.assertNotContains(banner, "hx-trigger")
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_refresh_at)

    def test_nothing_new(self):
        job = Refresh.objects.create(user=self.user)
        with mock.patch("sources.fetch.fetch_source", return_value=0):
            refresh.run(job)
        self.assertContains(self.client.get(reverse("feed:refresh_status", args=[job.pk])), "You're up to date")

    def test_failure_is_shown(self):
        job = Refresh.objects.create(user=self.user)
        with mock.patch("feed.scoring.score_new", side_effect=RuntimeError("boom")), \
                mock.patch("sources.fetch.fetch_source", return_value=0):
            refresh.run(job)
        self.assertContains(self.client.get(reverse("feed:refresh_status", args=[job.pk])), "didn't work")

    def test_manual_refresh_rechecks_sources_the_background_checked_recently(self):
        self.source.last_fetched_at = timezone.now() - timedelta(minutes=5)
        manual, scheduled = Refresh(trigger="manual"), Refresh(trigger="scheduled")
        self.assertTrue(refresh.should_fetch(self.source, manual))
        self.assertFalse(refresh.should_fetch(self.source, scheduled))
        self.source.last_fetched_at = timezone.now() - timedelta(seconds=30)
        self.assertFalse(refresh.should_fetch(self.source, manual))

    def test_other_people_cannot_see_a_refresh(self):
        job = Refresh.objects.create(user=self.user)
        self.client.force_login(User.objects.create_user(email="sam@example.com", setup_completed=True))
        self.assertEqual(self.client.get(reverse("feed:refresh_status", args=[job.pk])).status_code, 404)

    def test_reset_feed(self):
        a = self.article("ECB holds rates")
        ArticleScore.objects.create(user=self.user, article=a, score=80, story_key="ECB rate decision")
        for value in ("opened", "read", "up", "saved"):
            Rating.objects.create(user=self.user, article=a, value=value)
        self.user.briefing, self.user.briefing_date = [{"id": a.id}], timezone.localdate()
        self.user.save()
        other = User.objects.create_user(email="sam@example.com")
        ArticleScore.objects.create(user=other, article=a, score=50)
        with mock.patch.object(refresh, "refresh_in_background") as background:
            response = self.client.post(reverse("feed:reset_feed"))
        self.assertRedirects(response, reverse("feed:home"), fetch_redirect_response=False)
        background.assert_called_once()
        self.assertFalse(ArticleScore.objects.filter(user=self.user).exists())
        self.assertEqual(sorted(Rating.objects.filter(user=self.user).values_list("value", flat=True)), ["saved", "up"])
        self.assertTrue(ArticleScore.objects.filter(user=other).exists())  # only their own feed
        self.user.refresh_from_db()
        self.assertIsNone(self.user.briefing_date)
        self.assertContains(self.client.get(reverse("feed:settings")), "Reset my feed")

    def test_update_line(self):
        self.user.time_zone = "Europe/Berlin"
        self.user.refresh_times = ["07:00", "12:00"]
        berlin = ZoneInfo("Europe/Berlin")
        self.assertEqual(refresh.next_update(self.user, datetime(2026, 11, 20, 9, 0, tzinfo=berlin)),
                         datetime(2026, 11, 20, 12, 0, tzinfo=berlin))
        self.assertEqual(refresh.next_update(self.user, datetime(2026, 11, 20, 13, 0, tzinfo=berlin)),
                         datetime(2026, 11, 21, 7, 0, tzinfo=berlin))
        self.assertContains(self.client.get(reverse("feed:home")), "next update")

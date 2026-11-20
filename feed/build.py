"""Putting the feed together: ranking, discovery mix, filters, and the other sources of each story."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from sources.models import Article

from .models import ArticleScore, Rating

WINDOW = timedelta(days=3)
PAGE = 25
MAX_ITEMS = 400
# How much a story loses per hour of age when ranking Best for me, so fresh news rises.
AGE_PENALTY = 1.5
MIN_SCORE = 10
MIN_DISCOVERY_SCORE = 40
ALSO_SHOWN = 4  # other sources named on a card; the rest are counted


def read_reads(user, until=None):
    reads = Rating.objects.filter(user=user, value__in=Rating.READ_VALUES,
                                  created_at__gte=timezone.now() - WINDOW - timedelta(days=1))
    if until:
        reads = reads.filter(created_at__lte=until)
    return reads


def read_stories(user, until=None):
    """The stories (lead article ids) the person opened or marked as read, from any of their sources."""
    return {story or article for article, story in read_reads(user, until).values_list("article_id", "article__story_id")}


def read_labels(user, until=None):
    """Story labels the person has read an article of: {label lower: when}. Articles on that story ranked
    before then count as read too; ones ranked later are news on it and show up again."""
    when = {}
    for article, story, at in read_reads(user, until).values_list("article_id", "article__story_id", "created_at"):
        when[article] = max(when.get(article, at), at)
        if story:
            when[story] = max(when.get(story, at), at)
    labels = {}
    for article_id, label in ArticleScore.objects.filter(user=user, article_id__in=list(when)).exclude(
            story_key="").values_list("article_id", "story_key"):
        key = label.lower()
        labels[key] = max(labels.get(key, when[article_id]), when[article_id])
    return labels


def read_q(user, until=None):
    """A filter matching the person's read items: read articles, their duplicates, and their stories."""
    stories = read_stories(user, until)
    q = Q(article_id__in=stories) | Q(article__story_id__in=stories) if stories else Q(pk__in=[])
    for label, at in read_labels(user, until).items():
        q |= Q(story_key__iexact=label, created_at__lte=at)
    return q


def base(user, topic="", source="", q="", read=False, asof=None, story=""):
    """The person's visible items. Read stories are left out unless read=True. asof keeps a page-through
    stable: items scored and articles read after the first page was shown don't shift the later pages."""
    scores = (ArticleScore.objects.filter(user=user, hidden=False, article__published_at__gte=timezone.now() - WINDOW,
                                          score__gte=MIN_SCORE)
              .select_related("article__source"))
    if asof:
        scores = scores.filter(created_at__lte=asof)
    if not read:
        scores = scores.exclude(read_q(user, asof))
    if topic:
        scores = scores.filter(Q(topic__iexact=topic) | Q(new_topic__iexact=topic))
    if source:
        scores = scores.filter(article__source_id=source)
    if story:
        scores = scores.filter(story_key__iexact=story)
    if q:
        scores = scores.filter(Q(article__title__icontains=q) | Q(article__snippet__icontains=q))
    return scores


def rank(score, now):
    hours = (now - score.article.published_at).total_seconds() / 3600
    return score.score - AGE_PENALTY * min(hours, 72)


def mix(regular, discovery, share):
    """Puts one discovery item after every few regular ones, so about `share` percent are discoveries."""
    if not discovery or share <= 0:
        return regular
    every = max(1, round(100 / share) - 1)
    result, d = [], iter(discovery)
    for index, item in enumerate(regular, start=1):
        result.append(item)
        if index % every == 0:
            extra = next(d, None)
            if extra:
                result.append(extra)
    if len(result) == len(regular):  # a short feed still shows one discovery item, at the end
        result.append(discovery[0])
    return result


def read_count(user, topic="", source="", q="", asof=None, story="", **ignored):
    """How many stories are hidden because they were read."""
    every = base(user, topic, source, q, read=True, asof=asof, story=story)
    unread = base(user, topic, source, q, asof=asof, story=story)
    return len(groups(every)) - len(groups(unread))


def groups(scores):
    return {s.story_group for s in scores.select_related(None).only("story_key", "article_id", "article__story_id")}


def collapse(rows):
    """One card per story: the first (best ranked) item stands for it, the others go in its `more`."""
    leads, result = {}, []
    for s in rows:
        lead = leads.get(s.story_group)
        if lead is None:
            s.more = []
            leads[s.story_group] = s
            result.append(s)
        else:
            lead.more.append(s)
    return result


def items(user, mode="best", topic="", source="", q="", read=False, asof=None, story=""):
    """The person's feed as a list of ArticleScore (one per story), best or latest first."""
    scores = base(user, topic, source, q, read, asof, story)
    if story:  # one story: every article on it, newest first
        rows = list(scores.order_by("-article__published_at")[:MAX_ITEMS])
        for s in rows:
            s.more = []
        return rows
    if mode == "latest":
        return collapse(scores.order_by("-article__published_at")[:MAX_ITEMS])
    now = timezone.now()
    rows = list(scores.order_by("-score")[:MAX_ITEMS])
    if topic:  # a chosen topic shows its items, discovery or not
        return collapse(sorted(rows, key=lambda s: rank(s, now), reverse=True))
    rows = collapse(sorted(rows, key=lambda s: rank(s, now), reverse=True))
    regular = [s for s in rows if not s.is_discovery]
    discovery = [s for s in rows if s.is_discovery and s.score >= MIN_DISCOVERY_SCORE]
    return mix(regular, discovery, user.discovery_share)


def decorate(user, scores):
    """Adds what the cards show: other sources of the same story, the person's ratings, and new marks."""
    if not scores:
        return scores
    leads = {s.article.story_id or s.article_id for s in scores}
    others = defaultdict(list)
    for article in (Article.objects.filter(Q(story_id__in=leads) | Q(id__in=leads)).select_related("source")
                    .order_by("published_at")):
        others[article.story_id or article.id].append(article)
    ratings = defaultdict(set)
    for article_id, value in Rating.objects.filter(user=user, article_id__in=[s.article_id for s in scores]) \
            .values_list("article_id", "value"):
        ratings[article_id].add(value)
    # Opening the story from any of its sources counts as read.
    story_of = {a.id: lead for lead, articles in others.items() for a in articles}
    opened_stories = {story_of.get(a) for a in Rating.objects.filter(
        user=user, value__in=Rating.READ_VALUES, article_id__in=list(story_of)).values_list("article_id", flat=True)}
    labels_read = read_labels(user)
    for s in scores:
        lead = s.article.story_id or s.article_id
        seen = {s.article.source_name.lower()}
        s.also = []
        # The same article from other sources, then the other articles on the same story.
        for article in others[lead] + [m.article for m in getattr(s, "more", [])]:
            if article.id != s.article_id and article.source_name.lower() not in seen:
                seen.add(article.source_name.lower())
                s.also.append(article)
        s.also_shown, s.also_more = s.also[:ALSO_SHOWN], max(0, len(s.also) - ALSO_SHOWN)
        s.story_size = 1 + len(getattr(s, "more", []))
        s.ratings = ratings[s.article_id]
        read_at = labels_read.get(s.story_key.lower()) if s.story_key else None
        s.is_read = lead in opened_stories or bool(read_at and s.created_at <= read_at)
        s.is_new = bool(user.news_since and s.created_at > user.news_since)
    return scores


def page(user, number, asof=None, **filters):
    rows = items(user, asof=asof, **filters)
    start = (number - 1) * PAGE
    chunk = rows[start:start + PAGE]
    return decorate(user, chunk), len(rows) > start + PAGE

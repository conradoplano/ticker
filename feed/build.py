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


def base(user, topic="", source="", q=""):
    scores = (ArticleScore.objects.filter(user=user, hidden=False, article__published_at__gte=timezone.now() - WINDOW,
                                          score__gte=MIN_SCORE)
              .select_related("article__source"))
    if topic:
        scores = scores.filter(Q(topic__iexact=topic) | Q(new_topic__iexact=topic))
    if source:
        scores = scores.filter(article__source_id=source)
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


def items(user, mode="best", topic="", source="", q=""):
    """The person's feed as a list of ArticleScore, best or latest first."""
    scores = base(user, topic, source, q)
    if mode == "latest":
        return list(scores.order_by("-article__published_at")[:MAX_ITEMS])
    now = timezone.now()
    rows = list(scores.order_by("-score")[:MAX_ITEMS])
    if topic:  # a chosen topic shows its items, discovery or not
        return sorted(rows, key=lambda s: rank(s, now), reverse=True)
    regular = sorted([s for s in rows if not s.is_discovery], key=lambda s: rank(s, now), reverse=True)
    discovery = sorted([s for s in rows if s.is_discovery and s.score >= MIN_DISCOVERY_SCORE],
                       key=lambda s: rank(s, now), reverse=True)
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
    for s in scores:
        lead = s.article.story_id or s.article_id
        seen = {s.article.source_id}
        s.also = []
        for article in others[lead]:
            if article.id != s.article_id and article.source_id not in seen:
                seen.add(article.source_id)
                s.also.append(article)
        s.ratings = ratings[s.article_id]
        s.is_new = bool(user.news_since and s.created_at > user.news_since)
    return scores


def page(user, number, **filters):
    rows = items(user, **filters)
    start = (number - 1) * PAGE
    chunk = rows[start:start + PAGE]
    return decorate(user, chunk), len(rows) > start + PAGE

"""
The tools an assistant can use through MCP. Each works on the feed of the person who connected the
assistant, through the same functions as the web pages. Results are plain JSON-like dicts; a ToolError is
shown to the assistant as a failed call with its message.
"""
from dataclasses import dataclass

from django.urls import reverse

from feed import build, learning, refresh
from feed.models import ArticleScore, Rating
from feed.scoring import hide_subject
from sources.discover import discover
from sources.models import Article, UserSource
from sources.services import follow
from topics.models import Mute, Topic

TOOLS = {}


class ToolError(Exception):
    """Something the assistant should tell the person, or correct and try again."""


@dataclass
class Context:
    user: object
    base_url: str


def tool(name, title, description, properties=None, required=(), read_only=False, destructive=False,
         idempotent=False, open_world=False):
    def register(function):
        TOOLS[name] = {
            "function": function,
            "spec": {
                "name": name,
                "title": title,
                "description": description,
                "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                                "additionalProperties": False},
                "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                                "idempotentHint": idempotent, "openWorldHint": open_world},
            },
        }
        return function
    return register


def text(args, name, max_length, required=False):
    value = " ".join(str(args.get(name) or "").split())
    if required and not value:
        raise ToolError(f"{name} is required.")
    if len(value) > max_length:
        raise ToolError(f"{name} can be at most {max_length} characters.")
    return value


def number(args, name, low, high, required=False):
    value = args.get(name)
    if value is None or value == "":
        if required:
            raise ToolError(f"{name} is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"{name} must be a whole number from {low} to {high}.")
    return value




def item_info(context, score):
    article = score.article
    info = {
        "article_id": article.id,
        "title": article.title,
        "source": article.source_name,
        "published": article.published_at.isoformat(),
        "link": article.link,
        "summary": article.one_line or None,
        "score": score.score,
        "topic": score.topic or None,
        "new_topic": score.new_topic or None,
        "reason": score.reason,
        "rated": sorted(getattr(score, "ratings", set()) - {"opened", "read"}),
        "read": getattr(score, "is_read", False),
    }
    also = getattr(score, "also", [])
    if also:
        info["also_in"] = [a.source_name for a in also]
    if score.story_key:
        info["story"] = score.story_key
    return info


def match_topic(user, wanted):
    """The person's topic meant by `wanted`: exact, else one containing it or contained in it ("FIFA" ->
    "FIFA and football business"). Topics only seen as suggestions count too. "" if nothing fits."""
    if not wanted:
        return ""
    names = list(user.topics.values_list("name", flat=True))
    names += list(ArticleScore.objects.filter(user=user).exclude(new_topic="").values_list("new_topic", flat=True)
                  .distinct()[:200])
    low = wanted.lower()
    for name in names:
        if name.lower() == low:
            return name
    close = [n for n in names if low in n.lower() or n.lower() in low]
    return min(close, key=len) if close else ""


@tool("get_feed", "Get their news feed",
      "Their ranked feed of the last three days, one item per story: best for them first (or latest first), with "
      "each item's article_id, score (0-100), topic, story and a one-line summary. Stories they already read are "
      "left out unless include_read is true. Optionally one topic (their topic names, see the result's topics; "
      "close names work) or a search in titles and snippets.",
      {"order": {"type": "string", "enum": ["best", "latest"], "description": "best (default) or latest."},
       "topic": {"type": "string", "maxLength": 80, "description": "Only items about this topic."},
       "search": {"type": "string", "maxLength": 100, "description": "Only items whose title or snippet has this."},
       "include_read": {"type": "boolean", "description": "Also stories they already read (marked read: true)."},
       "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "How many (default 25)."}},
      read_only=True, idempotent=True)
def get_feed(context, args):
    user = context.user
    order = args.get("order") or "best"
    if order not in ("best", "latest"):
        raise ToolError("order must be best or latest.")
    limit = number(args, "limit", 1, 100) or 25
    wanted, search = text(args, "topic", 80), text(args, "search", 100)
    topic = match_topic(user, wanted)
    topics = list(user.topics.filter(active=True).values_list("name", flat=True))
    result = {"topics": topics, "feed_url": context.base_url + reverse("feed:home")}
    if wanted and not topic:
        result.update(items=[], note=f"They have no topic like \"{wanted}\". Their topics are listed in topics; "
                                     f"or use search to look for a word in titles.")
        return result
    read = bool(args.get("include_read"))
    rows = build.items(user, mode=order, topic=topic, q=search, read=read)
    notes = []
    if topic and topic.lower() != wanted.lower():
        notes.append(f"Showing their topic \"{topic}\".")
    if not rows and not read:
        rows = build.items(user, mode=order, topic=topic, q=search, read=True)
        if rows:
            notes.append("They have read all of these already; shown anyway, marked read: true.")
    if not rows:
        if search or topic:
            notes.append("Nothing in the last three days matches this.")
        elif refresh.running(user):
            notes.append("Their feed is being updated right now; try again in a minute.")
        else:
            notes.append("Nothing was ranked for them in the last three days. Check their sources with get_settings.")
    rows = build.decorate(user, rows[:limit])
    result["items"] = [item_info(context, s) for s in rows]
    if notes:
        result["note"] = " ".join(notes)
    return result


@tool("get_briefing", "Get today's briefing",
      "Today's morning briefing: the five most important items for them, one sentence each.",
      read_only=True, idempotent=True)
def get_briefing(context, args):
    user = context.user
    learning.ensure_briefing(user)
    return {"date": str(user.briefing_date), "items": [
        {"article_id": b["id"], "title": b["title"], "source": b["source"], "summary": b["sentence"]}
        for b in user.briefing]}


@tool("get_settings", "Get their topics and sources",
      "What the feed follows: topics (on or off), muted subjects, sources, refresh times and the profile learned "
      "from their ratings.", read_only=True, idempotent=True)
def get_settings(context, args):
    user = context.user
    return {
        "topics": [{"name": t.name, "active": t.active} for t in user.topics.all()],
        "muted": list(user.mutes.values_list("subject", flat=True)),
        "sources": [{"name": us.display_name, "feed_url": us.source.feed_url, "active": us.active}
                    for us in UserSource.objects.filter(user=user).select_related("source")],
        "refresh_times": user.refresh_times, "time_zone": user.time_zone,
        "learned_profile": user.learned_profile,
    }


@tool("add_topic", "Follow a topic",
      "Adds a topic to follow, in plain words (e.g. \"markets and central banks\"). Unmutes it if it was muted.",
      {"name": {"type": "string", "maxLength": 80}}, required=["name"], idempotent=True)
def add_topic(context, args):
    name = text(args, "name", 80, required=True)
    user = context.user
    topic, created = Topic.objects.get_or_create(user=user, name__iexact=name, defaults={"name": name})
    if not topic.active:
        topic.active = True
        topic.save(update_fields=["active"])
    Mute.objects.filter(user=user, subject__iexact=name).delete()
    ArticleScore.objects.filter(user=user, new_topic__iexact=name).update(topic=topic.name, new_topic="")
    return {"topic": topic.name, "added": created}


@tool("add_source", "Add a news source",
      "Follows a publication: give its name (e.g. \"Reuters\"), website or feed address. The app finds its RSS "
      "or Atom feed; publishers without a public feed can't be added. New articles arrive with the next refresh.",
      {"source": {"type": "string", "maxLength": 500, "description": "Name, website or feed address."}},
      required=["source"], open_world=True)
def add_source(context, args):
    query = text(args, "source", 500, required=True)
    if query.startswith("https://news.google.com/rss/search?"):
        from urllib.parse import parse_qs, urlsplit

        from sources.suggest import follow_suggestion
        topic = parse_qs(urlsplit(query).query).get("q", ["news"])[0]
        link, created = follow_suggestion(context.user, {"name": f"Topic: {topic}", "feed_url": query,
                                                         "site_url": "https://news.google.com/", "kind": "topic",
                                                         "topics": [topic]})
        return {"source": link.display_name, "feed_url": query, "added": created}
    found = discover(context.user, query.replace(",", " "))
    if not found or not found[0].ok:
        raise ToolError(found[0].error if found else "Nothing found.")
    result = found[0]
    link, created = follow(context.user, result.name, result.feed_url, result.site_url)
    return {"source": link.display_name, "feed_url": result.feed_url, "added": created}


@tool("suggest_sources", "Suggest sources for topics",
      "Finds sources for their topics (or one subject): publications with a working feed, and topic feeds (a news "
      "search for one topic, from many publishers). Takes up to a minute. Follow one with add_source and its "
      "feed_url.",
      {"subject": {"type": "string", "maxLength": 120,
                   "description": "One subject; empty: all their active topics."}},
      read_only=True, open_world=True)
def suggest_sources(context, args):
    from sources.suggest import suggest

    subject = text(args, "subject", 120)
    topics = [subject] if subject else list(context.user.topics.filter(active=True).values_list("name", flat=True))
    if not topics:
        raise ToolError("They follow no topics yet; give a subject or add a topic first.")
    return {"suggestions": [
        {"name": item["name"], "feed_url": item["feed_url"], "kind": item["kind"], "topics": item["topics"],
         "why": item["why"]}
        for item in suggest(context.user, topics, about=subject)]}


@tool("rate_item", "Rate an item",
      "Teaches the ranking: up (interesting), down (not interesting, hides it), or saved (keep for later). "
      "Set clear=true to take a rating back. Optionally mute_topic=true also stops the item's topic.",
      {"article_id": {"type": "integer"},
       "rating": {"type": "string", "enum": ["up", "down", "saved"]},
       "clear": {"type": "boolean"},
       "mute_topic": {"type": "boolean"}},
      required=["article_id", "rating"], idempotent=True)
def rate_item(context, args):
    user = context.user
    article_id = number(args, "article_id", 1, 2**62, required=True)
    value = args.get("rating")
    if value not in ("up", "down", "saved"):
        raise ToolError("rating must be up, down or saved.")
    article = Article.objects.filter(pk=article_id, scores__user=user).first()
    if article is None:
        raise ToolError("No such item in their feed; use get_feed for article_id.")
    if args.get("clear"):
        Rating.objects.filter(user=user, article=article, value=value).delete()
    else:
        if value in ("up", "down"):
            Rating.objects.filter(user=user, article=article, value__in=("up", "down")).delete()
        Rating.objects.get_or_create(user=user, article=article, value=value)
    score = ArticleScore.objects.get(user=user, article=article)
    score.hidden = Rating.objects.filter(user=user, article=article, value="down").exists()
    muted = None
    if args.get("mute_topic") and (score.topic or score.new_topic):
        muted = score.topic or score.new_topic
        Mute.objects.get_or_create(user=user, subject__iexact=muted, defaults={"subject": muted})
        Topic.objects.filter(user=user, name__iexact=muted).update(active=False)
        hide_subject(user, muted)
        score.hidden = True
    score.save(update_fields=["hidden"])
    return {"article_id": article.id, "ratings": sorted(
        Rating.objects.filter(user=user, article=article).exclude(value__in=Rating.READ_VALUES).values_list("value", flat=True)),
        "muted_topic": muted}


def call(name, context, args):
    return TOOLS[name]["function"](context, args)

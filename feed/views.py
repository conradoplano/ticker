import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import ai
from sources.models import Article, UserSource
from topics.models import Mute, Topic

from . import build, learning, refresh, scoring
from .models import ArticleScore, Rating

# Opening the app after this long away is a new visit; new items are marked from the visit before.
VISIT_GAP = timedelta(hours=1)


def setup_required(view):
    """Until the setup wizard is finished or skipped, it replaces the feed."""
    def wrapped(request, *args, **kwargs):
        if not request.user.setup_completed:
            return redirect("feed:setup")
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return login_required(wrapped)


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def note_visit(request):
    """Marks a new visit; returns True if this is one (after VISIT_GAP away)."""
    user = request.user
    now = timezone.now()
    new_visit = not user.last_visit_at or now - user.last_visit_at > VISIT_GAP
    if new_visit:
        user.news_since = user.last_visit_at
    user.last_visit_at = now
    user.save(update_fields=["last_visit_at", "news_since"])
    return new_visit


def filters(request):
    mode = request.GET.get("mode", "best")
    source = request.GET.get("source", "")
    return {
        "mode": mode if mode in ("best", "latest") else "best",
        "topic": request.GET.get("topic", "")[:80],
        "source": int(source) if source.isdigit() else "",
        "q": request.GET.get("q", "").strip()[:100],
        "read": request.GET.get("read") == "1",
        "story": " ".join(request.GET.get("story", "").split())[:120],
    }


def page_time(request):
    """When the first page was shown (kept by "more" links), so later pages don't shift."""
    try:
        return datetime.fromtimestamp(float(request.GET["asof"]), tz=dt_timezone.utc)
    except (KeyError, ValueError, OverflowError, OSError):
        return timezone.now()


@setup_required
def home(request):
    user = request.user
    if note_visit(request) and refresh.waiting(user):
        refresh.refresh_in_background(user)
    chosen = filters(request)
    try:
        number = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        number = 1
    asof = page_time(request)
    scores, more = build.page(user, number, asof=asof, **chosen)
    query = {k: v for k, v in chosen.items() if v and not (k == "mode" and v == "best")}
    if query.get("read"):
        query["read"] = 1
    context = {
        "scores": scores, "more": more, "next_page": number + 1, "f": chosen, "query": query,
        "asof": f"{asof.timestamp():.3f}",
    }
    if is_htmx(request) and number > 1:
        return render(request, "feed/_items.html", context)
    today = user.local_now().date()
    context.update({
        "topics": user.topics.filter(active=True),
        "follows": UserSource.objects.filter(user=user, active=True).select_related("source"),
        "show_briefing": not any(chosen[k] for k in ("topic", "source", "q", "story")) and chosen["mode"] == "best",
        "briefing_ready": user.briefing_date == today,
        "has_sources": UserSource.objects.filter(user=user, active=True).exists(),
        "read_hidden": 0 if chosen["read"] else build.read_count(user, asof=asof, **chosen),
        "topic_followed": bool(chosen["topic"]) and user.topics.filter(name__iexact=chosen["topic"], active=True).exists(),
    })
    if is_htmx(request):
        return render(request, "feed/_list.html", context)
    return render(request, "feed/home.html", context)


@setup_required
def briefing(request):
    """The morning briefing block, loaded after the page so the feed never waits for it."""
    user = request.user
    today = user.local_now().date()
    if user.briefing_date != today:
        learning.build_briefing(user)
    first_today = request.session.get("briefing_seen") != str(today)
    request.session["briefing_seen"] = str(today)
    return render(request, "feed/_briefing.html", {"items": user.briefing, "open": first_today,
                                                   "opened": opened_ids(user, [b["id"] for b in user.briefing])})


@setup_required
def open_article(request, pk):
    """Opening an article counts as interest; then off to the original page."""
    article = get_object_or_404(Article, pk=pk)
    Rating.objects.get_or_create(user=request.user, article=article, value=Rating.Value.OPENED)
    return redirect(article.link)


def card_response(request, score):
    if score is None or score.hidden:
        return HttpResponse("")
    build.decorate(request.user, [score])
    return render(request, "feed/_item.html", {"s": score})


@setup_required
@require_POST
def rate(request, pk):
    """👍, 👎 and Save for later. Tapping an active choice again takes it back."""
    user = request.user
    article = get_object_or_404(Article, pk=pk)
    value = request.POST.get("value")
    if value not in ("up", "down", "saved", "read"):
        return HttpResponse("Unknown rating", status=400)
    if value == "read":
        # Read or not, for the whole story: opening it from any source counts.
        lead = article.story_id or article.id
        story = Rating.objects.filter(user=user, value__in=Rating.READ_VALUES).filter(
            Q(article_id=lead) | Q(article__story_id=lead))
        existing = story if story.exists() else Rating.objects.none()
    else:
        existing = Rating.objects.filter(user=user, article=article, value=value)
    if existing.exists():
        existing.delete()
    else:
        if value in ("up", "down"):
            Rating.objects.filter(user=user, article=article, value__in=("up", "down")).delete()
        Rating.objects.create(user=user, article=article, value=value)
    score = ArticleScore.objects.filter(user=user, article=article).select_related("article__source").first()
    if score and value == "down":
        score.hidden = Rating.objects.filter(user=user, article=article, value="down").exists()
        score.save(update_fields=["hidden"])
    if not is_htmx(request):
        return redirect(request.POST.get("next") or "feed:home")
    if request.POST.get("where") == "saved":
        if not Rating.objects.filter(user=user, article=article, value="saved").exists():
            return HttpResponse("")
        return render(request, "feed/_saved_item.html", {"article": article, "opened": opened_ids(user, [article.id])})
    return card_response(request, score)


@setup_required
@require_POST
def follow_topic(request, pk):
    """+ Follow this topic, on a discovery item."""
    user = request.user
    score = get_object_or_404(ArticleScore, user=user, article_id=pk)
    name = score.new_topic or score.topic
    if name:
        topic, _ = Topic.objects.get_or_create(user=user, name__iexact=name, defaults={"name": name})
        if not topic.active:
            topic.active = True
            topic.save(update_fields=["active"])
        Mute.objects.filter(user=user, subject__iexact=name).delete()
        ArticleScore.objects.filter(user=user, new_topic__iexact=name).update(topic=topic.name, new_topic="")
        messages.success(request, f"You now follow {topic.name}.")
    score.refresh_from_db()
    if not is_htmx(request):
        return redirect("feed:home")
    return card_response(request, score)


@setup_required
@require_POST
def mute_topic(request, pk):
    """Mute topic: mutes the item's topic (or, without one, the subject the person types)."""
    user = request.user
    score = get_object_or_404(ArticleScore, user=user, article_id=pk)
    subject = " ".join((request.POST.get("subject") or score.topic or score.new_topic or "").split())[:80]
    if subject:
        Mute.objects.get_or_create(user=user, subject__iexact=subject, defaults={"subject": subject})
        Topic.objects.filter(user=user, name__iexact=subject).update(active=False)
        scoring.hide_subject(user, subject)
    Rating.objects.filter(user=user, article_id=pk, value="up").delete()
    Rating.objects.get_or_create(user=user, article_id=pk, value=Rating.Value.NOT_INTERESTED)
    ArticleScore.objects.filter(pk=score.pk).update(hidden=True)
    if is_htmx(request):
        response = HttpResponse("")
        if subject:
            response["HX-Trigger"] = json.dumps({"toast": f"Muted {subject}. You can undo it in Settings."})
        return response
    if subject:
        messages.success(request, f"Muted {subject}. You can undo it in Settings.")
    return redirect("feed:home")


@setup_required
@require_POST
def refresh_now(request):
    count = refresh.refresh_user(request.user)
    messages.success(request, f"{count} new item{'s' if count != 1 else ''}." if count else "Nothing new right now.")
    if is_htmx(request):
        response = HttpResponse("")
        response["HX-Redirect"] = reverse("feed:home")
        return response
    return redirect("feed:home")


@setup_required
def saved(request):
    articles = (Article.objects.filter(ratings__user=request.user, ratings__value="saved")
                .select_related("source").order_by("-ratings__created_at"))
    articles = list(articles)
    return render(request, "feed/saved.html", {"articles": articles,
                                               "opened": opened_ids(request.user, [a.id for a in articles])})


def opened_ids(user, article_ids):
    """Which of these articles the person has opened or marked as read."""
    return set(Rating.objects.filter(user=user, article_id__in=article_ids, value__in=Rating.READ_VALUES)
               .values_list("article_id", flat=True))


def stats(user):
    today = timezone.now().astimezone(user.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    sources = UserSource.objects.filter(user=user, active=True).values("source_id")
    scored = ArticleScore.objects.filter(user=user, created_at__gte=today)
    return {
        "fetched": Article.objects.filter(source_id__in=sources, fetched_at__gte=today).count(),
        "scored": scored.count(),
        "shown": scored.filter(hidden=False, score__gte=build.MIN_SCORE).count(),
        "ai_cost": ai.spent(ai.today_start(), user=user),
        "ai_month": ai.spent(ai.today_start().replace(day=1), user=user),
        "ai_limit": ai.daily_limit(user),
        "ai_blocked": ai.blocked(user),
    }


@login_required
def settings_page(request):
    from .forms import PreferencesForm, ProfileForm

    user = request.user
    prefs = PreferencesForm(instance=user)
    profile = ProfileForm(instance=user)
    if request.method == "POST":
        which = request.POST.get("form")
        if which == "prefs":
            prefs = PreferencesForm(request.POST, instance=user)
            if prefs.is_valid():
                prefs.save()
                messages.success(request, "Saved.")
                return redirect(reverse("feed:settings") + "#schedule")
        elif which == "profile":
            profile = ProfileForm(request.POST, instance=user)
            if profile.is_valid():
                profile.save()
                messages.success(request, "Saved.")
                return redirect(reverse("feed:settings") + "#profile")
    from connect.models import Connection

    return render(request, "feed/settings.html", {
        "prefs": prefs, "profile": profile,
        "follows": UserSource.objects.filter(user=user).select_related("source"),
        "topics": user.topics.all(), "mutes": user.mutes.all(),
        "stats": stats(user),
        "connections": Connection.objects.filter(user=user).select_related("client"),
        "mcp_url": request.build_absolute_uri("/mcp"),
    })


@login_required
@require_POST
def learn_now(request):
    user = request.user
    if request.POST.get("reset"):
        user.learned_profile = ""
        user.learned_at = timezone.now()
        user.save(update_fields=["learned_profile", "learned_at"])
        messages.success(request, "The learned profile was cleared.")
    else:
        try:
            learning.learn(user)
            messages.success(request, "The learned profile was updated from your ratings.")
        except ai.AIError as exc:
            messages.error(request, str(exc))
    return redirect(reverse("feed:settings") + "#profile")


@login_required
def export(request):
    user = request.user
    data = {
        "exported_at": timezone.now().isoformat(),
        "account": {"email": user.email, "name": user.name, "time_zone": user.time_zone,
                    "refresh_times": user.refresh_times, "discovery_share": user.discovery_share,
                    "profile_text": user.profile_text, "avoid_text": user.avoid_text,
                    "learned_profile": user.learned_profile},
        "topics": [{"name": t.name, "active": t.active} for t in user.topics.all()],
        "muted": list(user.mutes.values_list("subject", flat=True)),
        "sources": [{"name": f.display_name, "feed_url": f.source.feed_url, "active": f.active}
                    for f in UserSource.objects.filter(user=user).select_related("source")],
        "ratings": [{"value": r.value, "title": r.article.title, "link": r.article.link,
                     "source": r.article.source.name, "at": r.created_at.isoformat()}
                    for r in Rating.objects.filter(user=user).select_related("article__source")],
    }
    response = JsonResponse(data, json_dumps_params={"indent": 2, "ensure_ascii": False})
    name = settings.SITE_NAME.lower().replace(" ", "-")
    response["Content-Disposition"] = f'attachment; filename="{name}-export-{timezone.localdate()}.json"'
    return response


@login_required
def delete_account(request):
    if request.method == "POST":
        if request.POST.get("email", "").strip().lower() == request.user.email.lower():
            user = request.user
            logout(request)
            user.delete()
            messages.success(request, "Your account and all its data were deleted.")
            return redirect("accounts:login")
        messages.error(request, "Type your email address to confirm.")
    return render(request, "feed/delete.html")

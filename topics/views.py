"""Topics and muted subjects in Settings."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from feed.models import ArticleScore
from feed.scoring import hide_subject

from .models import Mute, Topic


def back():
    return redirect(reverse("feed:settings") + "#topics")


def clean(value):
    return " ".join((value or "").split())[:80]


@login_required
@require_POST
def add(request):
    for name in request.POST.get("name", "").split(","):
        name = clean(name)
        if not name:
            continue
        topic, created = Topic.objects.get_or_create(user=request.user, name__iexact=name, defaults={"name": name})
        if not created and not topic.active:
            topic.active = True
            topic.save(update_fields=["active"])
        Mute.objects.filter(user=request.user, subject__iexact=name).delete()
        ArticleScore.objects.filter(user=request.user, new_topic__iexact=name).update(topic=topic.name, new_topic="")
    return back()


@login_required
@require_POST
def toggle(request, pk):
    topic = get_object_or_404(Topic, pk=pk, user=request.user)
    topic.active = not topic.active
    topic.save(update_fields=["active"])
    return back()


@login_required
@require_POST
def remove(request, pk):
    topic = get_object_or_404(Topic, pk=pk, user=request.user)
    topic.delete()
    return back()


@login_required
@require_POST
def mute(request):
    subject = clean(request.POST.get("subject"))
    if subject:
        Mute.objects.get_or_create(user=request.user, subject__iexact=subject, defaults={"subject": subject})
        hidden = hide_subject(request.user, subject)
        if hidden:
            messages.info(request, f"Hid {hidden} item{'s' if hidden != 1 else ''} about {subject}.")
    return back()


@login_required
@require_POST
def unmute(request, pk):
    get_object_or_404(Mute, pk=pk, user=request.user).delete()
    return back()

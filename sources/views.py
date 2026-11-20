"""Sources in Settings: add by name or address (the app finds the feed and the person confirms), switch on
or off, rename and remove."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .discover import discover
from .models import UserSource
from .services import follow

PENDING = "found_sources"


def back():
    return redirect(reverse("feed:settings") + "#sources")


@login_required
def add(request):
    """Step one finds the feeds; step two (confirm) follows the ones ticked."""
    if request.method == "POST" and request.POST.get("action") == "confirm":
        found = {f["feed_url"]: f for f in request.session.pop(PENDING, []) if f.get("feed_url")}
        added = 0
        for url in request.POST.getlist("feed"):
            if url in found:
                follow(request.user, request.POST.get(f"name-{url}") or found[url]["name"], url, found[url]["site_url"])
                added += 1
        if added:
            messages.success(request, f"Added {added} source{'s' if added != 1 else ''}. Its news arrives with the next refresh.")
        return back()
    text = request.POST.get("names", "") if request.method == "POST" else ""
    if not text.strip():
        return back()
    results = [r.as_dict() for r in discover(request.user, text)]
    request.session[PENDING] = results
    return render(request, "sources/confirm.html", {"found": results})


@login_required
@require_POST
def toggle(request, pk):
    link = get_object_or_404(UserSource, pk=pk, user=request.user)
    link.active = not link.active
    link.save(update_fields=["active"])
    return back()


@login_required
@require_POST
def rename(request, pk):
    link = get_object_or_404(UserSource, pk=pk, user=request.user)
    name = " ".join(request.POST.get("name", "").split())[:120]
    link.name = "" if name == link.source.name else name
    link.save(update_fields=["name"])
    return back()


@login_required
@require_POST
def remove(request, pk):
    link = get_object_or_404(UserSource, pk=pk, user=request.user)
    messages.success(request, f"Removed {link.display_name}.")
    link.delete()
    return back()

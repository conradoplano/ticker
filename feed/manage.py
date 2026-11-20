"""The admin page (as in Chef): people waiting for AI, what each person's AI costs, limits and suspensions."""
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import User
from core import ai
from core.models import AIUsage


def staff_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def month_start():
    return ai.today_start().replace(day=1)


@staff_only
def manage(request):
    today, month = ai.today_start(), month_start()
    people = list(
        User.objects.annotate(
            cost_today=Sum("ai_usage__cost", filter=Q(ai_usage__created_at__gte=today)),
            cost_month=Sum("ai_usage__cost", filter=Q(ai_usage__created_at__gte=month)),
            cost_total=Sum("ai_usage__cost"),
            last_ai=Max("ai_usage__created_at"),
        ).order_by("-date_joined")
    )
    for person in people:
        person.limit = ai.daily_limit(person)
        person.over_limit = (person.cost_today or 0) >= person.limit
        person.ai_on = person.ai_approved or person.is_staff
    by_kind = (AIUsage.objects.values("kind")
               .annotate(calls=Count("pk"), month=Sum("cost", filter=Q(created_at__gte=month)), total=Sum("cost"))
               .order_by("-total"))
    labels = dict(AIUsage.Kind.choices)
    for row in by_kind:
        row["label"] = labels.get(row["kind"], row["kind"])
    return render(request, "feed/manage.html", {
        "pending": [p for p in people if not p.ai_on and p.is_active],
        "people": people,
        "by_model": (AIUsage.objects.values("model")
                     .annotate(calls=Count("pk"), month=Sum("cost", filter=Q(created_at__gte=month)), total=Sum("cost"))
                     .order_by("-total")),
        "by_kind": by_kind,
        "today": ai.spent(today),
        "month": ai.spent(month),
        "total": ai.spent(),
        "global_limit": settings.AI_GLOBAL_DAILY_LIMIT_USD,
        "default_limit": settings.AI_DAILY_LIMIT_USD,
        "ai_configured": bool(settings.OPENAI_API_KEY),
        # Calls whose model has no price: they count as $0 against the limits.
        "unpriced": AIUsage.objects.filter(cost=0, input_tokens__gt=0).values("model").annotate(calls=Count("pk")),
    })


@staff_only
@require_POST
def manage_person(request, pk):
    person = get_object_or_404(User, pk=pk)
    action = request.POST.get("action")
    if action in ("suspend", "revoke") and person.pk == request.user.pk:
        messages.error(request, "That's you.")
    elif action == "approve":
        person.ai_approved = True
        person.save(update_fields=["ai_approved"])
        messages.success(request, f"{person} can use AI now.")
    elif action == "revoke":
        person.ai_approved = False
        person.save(update_fields=["ai_approved"])
        messages.success(request, f"{person} can't use AI any more." + (" (Admins always can.)" if person.is_staff else ""))
    elif action == "suspend":
        person.is_active = False
        person.save(update_fields=["is_active"])
        messages.success(request, f"Suspended {person}: they can't log in and their feed is no longer refreshed.")
    elif action == "reactivate":
        person.is_active = True
        person.save(update_fields=["is_active"])
        messages.success(request, f"{person} can log in again.")
    elif action == "limit":
        value = request.POST.get("limit", "").strip()
        try:
            limit = Decimal(value).quantize(Decimal("0.01")) if value else None
        except InvalidOperation:
            limit = Decimal(-1)
        if limit is not None and not 0 <= limit < 10000:
            messages.error(request, "Enter the limit in dollars, e.g. 0.50 – or leave it empty for the default.")
        else:
            person.ai_daily_limit = limit
            person.save(update_fields=["ai_daily_limit"])
            messages.success(request, f"{person}: AI limit is {'$' + str(limit) if limit is not None else 'the default'} per day.")
    return redirect(f"/manage/#person-{person.pk}")

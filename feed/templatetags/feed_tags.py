from django import template
from django.utils import timezone
from django.utils.timesince import timesince

register = template.Library()


@register.filter
def ago(moment):
    """"5 minutes ago", "3 hours ago": only the largest unit."""
    if not moment:
        return ""
    if timezone.now() - moment < timezone.timedelta(minutes=1):
        return "just now"
    return f"{timesince(moment, depth=1)} ago"


@register.filter
def update_time(moment):
    """"10:32", "yesterday 21:00" or "Mon 07:00", in the time zone the moment carries (the person's)."""
    if not moment:
        return ""
    today = timezone.now().astimezone(moment.tzinfo).date() if timezone.is_aware(moment) else timezone.localdate()
    day = moment.date()
    if day == today:
        return f"{moment:%H:%M}"
    if day == today - timezone.timedelta(days=1):
        return f"yesterday {moment:%H:%M}"
    if day == today + timezone.timedelta(days=1):
        return f"tomorrow {moment:%H:%M}"
    return f"{moment:%a %H:%M}"


@register.simple_tag(takes_context=True)
def query(context, **changes):
    """The current feed filters with some changed, as a query string."""
    params = dict(context.get("query") or {})
    for key, value in changes.items():
        if value in ("", None):
            params.pop(key, None)
        else:
            params[key] = value
    from urllib.parse import urlencode

    return "?" + urlencode(params) if params else "?"

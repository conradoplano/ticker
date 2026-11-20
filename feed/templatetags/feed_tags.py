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

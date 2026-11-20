from django.db import transaction

from .models import Source, UserSource


@transaction.atomic
def follow(user, name, feed_url, site_url=""):
    """Follows a feed (stored once for everyone). Returns (UserSource, created)."""
    name = " ".join((name or "").split())[:120] or feed_url
    source, _ = Source.objects.get_or_create(feed_url=feed_url, defaults={"name": name, "site_url": site_url or ""})
    if not source.active:
        source.active = True
        source.error_count = 0
        source.save(update_fields=["active", "error_count"])
    follow, created = UserSource.objects.get_or_create(
        user=user, source=source, defaults={"name": name if name != source.name else ""})
    if not created and not follow.active:
        follow.active = True
        follow.save(update_fields=["active"])
    return follow, created

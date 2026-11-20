"""
News sources (RSS or Atom feeds) and the articles fetched from them.

A source is stored once and shared by everyone who follows it, so it is fetched once for all of them.
Articles keep only what a feed gives away anyway: title, snippet, link, image link and time. The original
page is opened for reading; full articles are never copied.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class Source(models.Model):
    name = models.CharField(max_length=120)
    feed_url = models.URLField(max_length=500, unique=True)
    site_url = models.URLField(max_length=500, blank=True)
    active = models.BooleanField(default=True, help_text="Off: not fetched for anyone.")
    created_at = models.DateTimeField(default=timezone.now)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    error_count = models.PositiveIntegerField(default=0, help_text="Failed fetches in a row.")
    last_error = models.CharField(max_length=300, blank=True)
    # Conditional requests: sent back so an unchanged feed costs the publisher almost nothing.
    etag = models.CharField(max_length=300, blank=True)
    last_modified = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class UserSource(models.Model):
    """A person following a source."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_sources")
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="followers")
    name = models.CharField(max_length=120, blank=True, help_text="Their own name for it; empty: the source's.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["source__name"]
        constraints = [models.UniqueConstraint(fields=["user", "source"], name="one_follow_per_source")]

    def __str__(self):
        return f"{self.user} follows {self.source}"

    @property
    def display_name(self):
        return self.name or self.source.name


class Article(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="articles")
    guid = models.CharField(max_length=500, help_text="The feed's id for it, or its link.")
    title = models.CharField(max_length=500)
    snippet = models.TextField(blank=True)
    link = models.URLField(max_length=1000)
    image_url = models.URLField(max_length=1000, blank=True)
    published_at = models.DateTimeField(db_index=True)
    fetched_at = models.DateTimeField(default=timezone.now, db_index=True)
    # Normalised title, to find the same story from other sources.
    fingerprint = models.CharField(max_length=64, db_index=True)
    # The first article about the same story; the feed shows that one with the others listed underneath.
    story = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")

    class Meta:
        ordering = ["-published_at"]
        constraints = [models.UniqueConstraint(fields=["source", "guid"], name="one_article_per_guid")]

    def __str__(self):
        return self.title

    @property
    def lead(self):
        """The article that stands for its story."""
        return self.story or self

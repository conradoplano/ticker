from django.conf import settings
from django.db import models
from django.utils import timezone

from sources.models import Article


class ArticleScore(models.Model):
    """How relevant one article is for one person, and why."""

    class Method(models.TextChoices):
        AI = "ai", "AI"
        KEYWORDS = "keywords", "Topic keywords"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scores")
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="scores")
    score = models.PositiveSmallIntegerField(default=0, help_text="0 to 100.")
    topic = models.CharField(max_length=80, blank=True, help_text="The followed topic it matched.")
    new_topic = models.CharField(max_length=80, blank=True, help_text="A topic not followed yet (discovery).")
    reason = models.CharField(max_length=300, blank=True)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.AI)
    hidden = models.BooleanField(default=False, help_text="Rated not interesting, or its topic was muted.")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    shown_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-score", "-article__published_at"]
        constraints = [models.UniqueConstraint(fields=["user", "article"], name="one_score_per_article")]
        indexes = [models.Index(fields=["user", "hidden", "-score"])]

    def __str__(self):
        return f"{self.score} for {self.user}: {self.article}"

    @property
    def is_discovery(self):
        return bool(self.new_topic) and not self.topic


class Rating(models.Model):
    """What a person did with an article: the signals the feed learns from."""

    class Value(models.TextChoices):
        INTERESTED = "up", "Interesting"
        NOT_INTERESTED = "down", "Not interesting"
        OPENED = "opened", "Opened"
        SAVED = "saved", "Saved for later"
        READ = "read", "Marked as read"  # only greys it out; not a sign of interest

    # Either of these means the person has read it.
    READ_VALUES = ("opened", "read")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ratings")
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="ratings")
    value = models.CharField(max_length=10, choices=Value.choices)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["user", "article", "value"], name="one_rating_per_value")]

    def __str__(self):
        return f"{self.user} {self.value} {self.article}"

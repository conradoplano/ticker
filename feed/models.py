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
    # A short label for the news story it is about, shared by all articles on the same story ("FIFA presidential
    # race"), so the feed shows one card per story. Empty: not labelled (then the article's own story).
    story_key = models.CharField(max_length=120, blank=True, db_index=True)
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
    def story_group(self):
        """What groups it with other cards: its story label, else its article's story."""
        return self.story_key.lower() if self.story_key else f"#{self.article.story_id or self.article_id}"

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


class Refresh(models.Model):
    """One update of a person's feed, run in the background (as Chef runs its AI jobs): the page shows its
    progress and what it added."""

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Refresh now"
        VISIT = "visit", "Opened the app"
        SCHEDULED = "scheduled", "Refresh time"
        SETUP = "setup", "Setup"

    class Status(models.TextChoices):
        FETCHING = "fetching", "Checking sources"
        RANKING = "ranking", "Ranking new articles"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="refreshes")
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.FETCHING)
    sources_total = models.PositiveIntegerField(default=0)
    sources_checked = models.PositiveIntegerField(default=0)
    articles_fetched = models.PositiveIntegerField(default=0, help_text="New articles downloaded from their sources.")
    ranked = models.PositiveIntegerField(default=0, help_text="Articles ranked for them.")
    new_items = models.PositiveIntegerField(default=0, help_text="Of those, shown in the feed.")
    new_stories = models.PositiveIntegerField(default=0, help_text="The stories those belong to.")
    error = models.CharField(max_length=300, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_trigger_display()} for {self.user} at {self.started_at:%Y-%m-%d %H:%M}: {self.status}"

    @property
    def finished(self):
        return self.status in (self.Status.DONE, self.Status.FAILED)

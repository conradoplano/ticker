from django.conf import settings
from django.db import models
from django.utils import timezone


class AIUsage(models.Model):
    """What one call to the AI cost, for the daily limits and the admin page."""

    class Kind(models.TextChoices):
        SETUP = "setup", "Setup"
        SCORE = "score", "Scoring"
        LEARN = "learn", "Learned profile"
        BRIEFING = "briefing", "Briefing"
        SOURCES = "sources", "Source suggestions"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="ai_usage")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    model = models.CharField(max_length=50, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField("cost (USD)", max_digits=8, decimal_places=4, default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI usage"
        verbose_name_plural = "AI usage"

    def __str__(self):
        return f"{self.get_kind_display()} for {self.user} at {self.created_at:%Y-%m-%d %H:%M}: ${self.cost}"

from django.conf import settings
from django.db import models
from django.utils import timezone


class Topic(models.Model):
    """Something a person follows, in their own words ("markets and central banks", "FC Basel")."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="topics")
    name = models.CharField(max_length=80)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="one_topic_per_name")]

    def __str__(self):
        return self.name


class Mute(models.Model):
    """A subject a person never wants to see ("celebrity news", "local crime")."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mutes")
    subject = models.CharField(max_length=80)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["subject"]
        constraints = [models.UniqueConstraint(fields=["user", "subject"], name="one_mute_per_subject")]

    def __str__(self):
        return self.subject

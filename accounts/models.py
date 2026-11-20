from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Users are identified by email and never have a usable password."""

    def create_user(self, email, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, **extra_fields)


def default_refresh_times():
    return list(DEFAULT_REFRESH_TIMES)


DEFAULT_REFRESH_TIMES = ["07:00", "12:00", "17:00", "21:00"]


class User(AbstractBaseUser, PermissionsMixin):
    class Discovery(models.IntegerChoices):
        LOW = 5, "Low (about 1 in 20)"
        MEDIUM = 10, "Medium (about 1 in 10)"
        HIGH = 20, "High (about 1 in 5)"

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    # Opening the app after a while away is a visit; items scored since the visit before are new.
    last_visit_at = models.DateTimeField(null=True, blank=True)
    news_since = models.DateTimeField(null=True, blank=True)

    # Feed settings.
    time_zone = models.CharField(max_length=64, default="Europe/Berlin")
    refresh_times = models.JSONField(
        default=default_refresh_times, blank=True, help_text='Local times the feed updates, e.g. ["07:00", "12:00"].')
    discovery_share = models.PositiveSmallIntegerField(
        "discovery share (%)", default=Discovery.MEDIUM, help_text="Share of feed items from topics not followed yet.")
    profile_text = models.TextField("what I want", blank=True, max_length=1000)
    avoid_text = models.TextField("what I do not want", blank=True, max_length=500)
    learned_profile = models.TextField("what the app has learned", blank=True, max_length=2000)
    learned_at = models.DateTimeField(null=True, blank=True)
    setup_completed = models.BooleanField(default=False)
    # Last time this person's feed was scored, and the day of their last morning briefing.
    last_refresh_at = models.DateTimeField(null=True, blank=True)
    last_slot = models.CharField(max_length=20, blank=True, help_text="The scheduled refresh last run (date and time).")
    briefing_date = models.DateField(null=True, blank=True)
    briefing = models.JSONField(default=list, blank=True)

    # AI costs money: it is used for a person once an admin approves them.
    ai_approved = models.BooleanField("AI approved", default=False)
    ai_daily_limit = models.DecimalField(
        "AI limit per day (USD)", max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Empty: the app's default. 0: no AI.",
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.name or self.email

    def get_full_name(self):
        return self.name or self.email

    def get_short_name(self):
        return self.name.split(" ")[0] if self.name else self.email.split("@")[0]

    @property
    def tz(self):
        try:
            return ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(settings.TIME_ZONE)

    def local_now(self):
        return timezone.now().astimezone(self.tz)


class LoginCodeRequest(models.Model):
    """One login code requested for an email address (known or not), to limit how many are sent."""

    email = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} at {self.created_at:%Y-%m-%d %H:%M}"

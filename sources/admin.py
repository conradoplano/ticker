from django.contrib import admin

from .models import Article, Source, UserSource


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("name", "feed_url", "active", "last_fetched_at", "error_count", "last_error")
    list_filter = ("active",)
    search_fields = ("name", "feed_url")
    readonly_fields = ("last_fetched_at", "last_ok_at", "etag", "last_modified")


@admin.register(UserSource)
class UserSourceAdmin(admin.ModelAdmin):
    list_display = ("user", "source", "name", "active")
    list_filter = ("active",)
    search_fields = ("user__email", "source__name")


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "published_at", "story")
    list_filter = ("source",)
    search_fields = ("title",)
    raw_id_fields = ("story",)
    date_hierarchy = "published_at"

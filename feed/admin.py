from django.contrib import admin

from .models import ArticleScore, Rating


@admin.register(ArticleScore)
class ArticleScoreAdmin(admin.ModelAdmin):
    list_display = ("article", "user", "score", "topic", "new_topic", "method", "hidden", "created_at")
    list_filter = ("method", "hidden")
    search_fields = ("article__title", "user__email", "topic")
    raw_id_fields = ("article",)


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ("article", "user", "value", "created_at")
    list_filter = ("value",)
    search_fields = ("article__title", "user__email")
    raw_id_fields = ("article",)

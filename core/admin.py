from django.contrib import admin

from .models import AIUsage


@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "model", "input_tokens", "output_tokens", "cost")
    list_filter = ("kind", "model")
    search_fields = ("user__email",)
    date_hierarchy = "created_at"

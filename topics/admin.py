from django.contrib import admin

from .models import Mute, Topic


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "active")
    list_filter = ("active",)
    search_fields = ("name", "user__email")


@admin.register(Mute)
class MuteAdmin(admin.ModelAdmin):
    list_display = ("subject", "user")
    search_fields = ("subject", "user__email")

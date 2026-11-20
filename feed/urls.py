from django.urls import path

from . import manage, setup, views

app_name = "feed"

urlpatterns = [
    path("", views.home, name="home"),
    path("briefing/", views.briefing, name="briefing"),
    path("refresh/", views.refresh_now, name="refresh"),
    path("refresh/<int:pk>/", views.refresh_status, name="refresh_status"),
    path("saved/", views.saved, name="saved"),
    path("a/<int:pk>/open/", views.open_article, name="open"),
    path("a/<int:pk>/rate/", views.rate, name="rate"),
    path("a/<int:pk>/follow/", views.follow_topic, name="follow_topic"),
    path("a/<int:pk>/mute/", views.mute_topic, name="mute_topic"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/learn/", views.learn_now, name="learn"),
    path("settings/reset-feed/", views.reset_feed, name="reset_feed"),
    path("settings/export/", views.export, name="export"),
    path("settings/delete/", views.delete_account, name="delete"),
    path("manage/", manage.manage, name="manage"),
    path("manage/person/<int:pk>/", manage.manage_person, name="manage_person"),
    path("setup/", setup.describe, name="setup"),
    path("setup/topics/", setup.topics, name="setup_topics"),
    path("setup/sources/", setup.sources, name="setup_sources"),
    path("setup/sources/suggest/", setup.sources_suggest, name="setup_sources_suggest"),
    path("setup/preferences/", setup.preferences, name="setup_preferences"),
    path("setup/build/", setup.build_page, name="setup_build"),
    path("setup/build/run/", setup.build_run, name="setup_build_run"),
    path("setup/skip/", setup.skip, name="setup_skip"),
    path("setup/restart/", setup.restart, name="setup_restart"),
]

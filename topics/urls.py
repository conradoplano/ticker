from django.urls import path

from . import views

app_name = "topics"

urlpatterns = [
    path("settings/topics/add/", views.add, name="add"),
    path("settings/topics/<int:pk>/toggle/", views.toggle, name="toggle"),
    path("settings/topics/<int:pk>/remove/", views.remove, name="remove"),
    path("settings/mutes/add/", views.mute, name="mute"),
    path("settings/mutes/<int:pk>/remove/", views.unmute, name="unmute"),
]

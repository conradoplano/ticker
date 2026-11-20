from django.urls import path

from . import views

app_name = "sources"

urlpatterns = [
    path("settings/sources/add/", views.add, name="add"),
    path("settings/sources/suggest/", views.suggestions, name="suggest"),
    path("settings/sources/suggest/results/", views.suggestion_results, name="suggest_results"),
    path("settings/sources/<int:pk>/toggle/", views.toggle, name="toggle"),
    path("settings/sources/<int:pk>/rename/", views.rename, name="rename"),
    path("settings/sources/<int:pk>/remove/", views.remove, name="remove"),
]

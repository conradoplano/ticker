from django.urls import path

from . import views

app_name = "sources"

urlpatterns = [
    path("settings/sources/add/", views.add, name="add"),
    path("settings/sources/<int:pk>/toggle/", views.toggle, name="toggle"),
    path("settings/sources/<int:pk>/rename/", views.rename, name="rename"),
    path("settings/sources/<int:pk>/remove/", views.remove, name="remove"),
]

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("core.urls")),
    path("", include("feed.urls")),
    path("", include("sources.urls")),
    path("", include("topics.urls")),
    path("", include("connect.urls")),
]

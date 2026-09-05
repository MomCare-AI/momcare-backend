from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from momcare_platform.core.common.health import health_check
from momcare_platform.core.common.storage import file_blob_download

urlpatterns = [
    # Root + health check both return the service status (public, no auth) so the
    # base URL is never empty — hitting "/" gives the same readiness response.
    path("", health_check, name="root"),
    path("health/", health_check, name="health-check"),
    # Django Admin, use {% url 'admin:index' %}
    path(settings.ADMIN_URL, admin.site.urls),
    # API
    path("api/", include("config.api_router")),
    path("api/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
    # Files stored via DatabaseStorage (core/common/storage.py) — survive a
    # redeploy because they live in Postgres, not on this container's disk.
    # A path converter alone won't do: names carry the same nested
    # upload_to directories as disk storage did ("licenses/2026/09/<uuid>.pdf").
    re_path(r"^media/db/(?P<name>.+)$", file_blob_download, name="file-blob-download"),
    # Media files still on local disk (dev fallback, or anything not yet
    # migrated to DatabaseStorage).
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

if settings.DEBUG:
    # Serve static files via runserver/WhiteNoise in development.
    urlpatterns += staticfiles_urlpatterns()

    if "debug_toolbar" in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls)), *urlpatterns]

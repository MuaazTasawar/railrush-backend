from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("api/health/", include("apps.common.urls")),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/users/", include("apps.accounts.profile_urls")),
    path("api/catalog/", include("apps.catalog.urls")),
    path("api/waitroom/", include("apps.waitroom.urls")),
    # Prometheus metrics live under /internal/. Nginx blocks this prefix from the public (Phase 2).
    path("internal/", include("django_prometheus.urls")),
]

if settings.API_DOCS_ENABLED:
    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    ]

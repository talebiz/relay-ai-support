"""
URL configuration for core project.
"""
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from accounts.views import MeView
from health.views import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health_check, name="health-check"),
    # --- Auth ---
    # Login/refresh are handled entirely by simplejwt; Django is the
    # only component in this system that ever issues or verifies a
    # token. See core/settings.py SIMPLE_JWT for lifetimes.
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("api/auth/me/", MeView.as_view(), name="me"),
    # --- Domain ---
    path("api/", include("orders.urls")),
]

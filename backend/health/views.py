"""
Minimal health-check endpoint.

Used only to confirm that docker compose brought the backend service
up correctly and that it can talk to the database. Not part of the
project's actual domain logic - safe to remove or expand later.
"""

from django.db import connection
from django.http import JsonResponse


def health_check(request):
    """Return service status, including a basic database connectivity check."""
    db_ok = True
    db_error = None

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
    except Exception as exc:  # noqa: BLE001 - we want to report any DB error here
        db_ok = False
        db_error = str(exc)

    payload = {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
    }
    if db_error:
        payload["database_error"] = db_error

    return JsonResponse(payload, status=200 if db_ok else 503)

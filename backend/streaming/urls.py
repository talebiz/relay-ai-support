from django.urls import path

from streaming import views

urlpatterns = [
    path(
        "stream/<uuid:correlation_id>/",
        views.conversation_stream,
        name="conversation-stream",
    ),
]

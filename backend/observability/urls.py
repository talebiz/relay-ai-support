from django.urls import path

from observability import views

urlpatterns = [
    path(
        "conversations/<uuid:conversation_id>/usage/",
        views.ConversationUsageView.as_view(),
        name="conversation-usage",
    ),
]

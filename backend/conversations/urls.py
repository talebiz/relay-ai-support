from django.urls import path

from conversations import views

urlpatterns = [
    path(
        "orders/<uuid:order_id>/conversation/",
        views.OrderConversationView.as_view(),
        name="order-conversation",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        views.ConversationMessagesView.as_view(),
        name="conversation-messages",
    ),
]

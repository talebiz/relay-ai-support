from django.urls import path

from support import views

urlpatterns = [
    path("support/messages/", views.SendMessageView.as_view(), name="send-message"),
]

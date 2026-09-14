from django.urls import path

from documents import views

urlpatterns = [
    path("documents/", views.DocumentListCreateView.as_view(), name="document-list-create"),
    path("documents/<uuid:document_id>/", views.DocumentDetailView.as_view(), name="document-detail"),
]

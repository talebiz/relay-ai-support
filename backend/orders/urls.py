from django.urls import path

from orders import views

urlpatterns = [
    path("products/", views.ProductListView.as_view(), name="product-list"),
    path("orders/", views.OrderListView.as_view(), name="order-list"),
    path("orders/<uuid:order_id>/", views.OrderDetailView.as_view(), name="order-detail"),
    path(
        "orders/<uuid:order_id>/refunds/",
        views.RefundHistoryView.as_view(),
        name="order-refund-history",
    ),
    path("refunds/", views.RefundRequestListCreateView.as_view(), name="refund-list-create"),
]

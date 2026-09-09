"""
Views for the orders domain.

Every view below resolves data exclusively through orders/services.py,
passing `request.user` (the user DRF's JWT authentication resolved,
not anything read from the request body). No view here ever does
`Order.objects.get(id=...)` or `Order.objects.all()` directly - see
services.py for why that matters.
"""

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from orders import services
from orders.models import Product
from orders.serializers import (
    OrderSerializer,
    ProductSerializer,
    RefundRequestCreateSerializer,
    RefundRequestSerializer,
)


class ProductListView(APIView):
    """Public-ish catalog; still requires login since there's no anonymous flow."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        products = Product.objects.all().order_by("name")
        return Response(ProductSerializer(products, many=True).data)


class OrderListView(APIView):
    """
    GET /api/orders/

    Returns only the authenticated user's orders (or all orders, if the
    caller is a support agent) - see services.orders_visible_to.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        orders = services.orders_visible_to(request.user)
        return Response(OrderSerializer(orders, many=True).data)


class OrderDetailView(APIView):
    """
    GET /api/orders/<uuid:order_id>/

    404s (not 403) for another user's order, so we never confirm to a
    caller that a given order id exists at all outside their own scope.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        try:
            order = services.get_order_for_user(request.user, order_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Order not found.") from exc
        return Response(OrderSerializer(order).data)


class RefundHistoryView(APIView):
    """GET /api/orders/<uuid:order_id>/refunds/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        try:
            refunds = services.refund_history_for_order(request.user, order_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Order not found.") from exc
        return Response(RefundRequestSerializer(refunds, many=True).data)


class RefundRequestListCreateView(APIView):
    """
    GET  /api/refunds/   - list this user's refund requests
    POST /api/refunds/   - file a new refund request (the "action" tool
                           the project brief requires at least one of)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        refunds = services.refund_requests_visible_to(request.user)
        return Response(RefundRequestSerializer(refunds, many=True).data)

    def post(self, request):
        serializer = RefundRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            refund = services.create_refund_request(
                acting_user=request.user,
                order_id=serializer.validated_data["order_id"],
                reason=serializer.validated_data["reason"],
            )
        except ObjectDoesNotExist as exc:
            raise NotFound("Order not found.") from exc
        return Response(RefundRequestSerializer(refund).data, status=status.HTTP_201_CREATED)

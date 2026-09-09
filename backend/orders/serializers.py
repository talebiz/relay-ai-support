from rest_framework import serializers

from orders.models import Order, Product, RefundRequest


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "name", "sku", "description", "price_cents"]


class OrderSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    total_cents = serializers.IntegerField(read_only=True)
    # Exposed only so a support agent viewing another user's order can
    # tell whose it is; customers only ever see their own rows anyway.
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "username",
            "product",
            "quantity",
            "status",
            "tracking_number",
            "total_cents",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RefundRequestSerializer(serializers.ModelSerializer):
    order_id = serializers.UUIDField(source="order.id", read_only=True)

    class Meta:
        model = RefundRequest
        fields = ["id", "order_id", "reason", "status", "resolution_note", "created_at"]
        read_only_fields = ["id", "order_id", "status", "resolution_note", "created_at"]


class RefundRequestCreateSerializer(serializers.Serializer):
    """
    Input serializer for creating a refund request. `order_id` is only
    ever resolved against the requesting user's own orders inside
    orders.services.create_refund_request - accepting it here does not
    grant access to arbitrary orders.
    """

    order_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000)

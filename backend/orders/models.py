"""
Domain models: Product, Order, RefundRequest.

These mirror what the reference course built (see project doc, section 4
comparison table), but the course's Django-template version has no real
authorization boundary between users - it's a single-tenant teaching demo.
Here, every model that belongs to a customer carries a `user` FK, and
every query path that returns rows to a customer is required to filter
on it. See orders/services.py for where that filtering actually happens,
and orders/permissions.py + accounts/permissions.py for why it happens
in code, not in an LLM system prompt.
"""

import uuid

from django.conf import settings
from django.db import models


class Product(models.Model):
    """
    Products are not owned by a single user - everyone can see the
    catalog. No per-user filtering needed here; it's the Order and
    RefundRequest models below where authorization actually matters.
    """

    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True)
    price_cents = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class OrderStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SHIPPED = "shipped", "Shipped"
    DELIVERED = "delivered", "Delivered"
    CANCELLED = "cancelled", "Cancelled"


class Order(models.Model):
    """
    Belongs to exactly one user. Every query that lists or fetches
    Orders MUST go through OrderQuerySet.visible_to(user) (below) or
    orders/services.py - never `Order.objects.all()` reachable from a
    view or a tool.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="orders")
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING
    )
    tracking_number = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"Order {self.id} ({self.user_id})"

    @property
    def total_cents(self):
        return self.product.price_cents * self.quantity


class RefundStatus(models.TextChoices):
    REQUESTED = "requested", "Requested"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    REFUNDED = "refunded", "Refunded"


class RefundRequest(models.Model):
    """
    Belongs to the order's user. We denormalize `user` onto this model
    (instead of only reaching it via order.user) so that filtering by
    the authenticated user is a single indexed column lookup, not a
    join that's easy to forget in a hand-written query.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="refund_requests"
    )
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="refund_requests")
    reason = models.TextField()
    status = models.CharField(
        max_length=20, choices=RefundStatus.choices, default=RefundStatus.REQUESTED
    )
    resolution_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"Refund {self.id} for order {self.order_id}"

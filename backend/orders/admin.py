from django.contrib import admin

from orders.models import Order, Product, RefundRequest


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "price_cents")
    search_fields = ("name", "sku")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    # The Django admin is a superuser tool, not a customer-facing surface,
    # so it intentionally bypasses orders.services and shows everything -
    # that's expected and fine here, unlike in views.py or any AI tool.
    list_display = ("id", "user", "product", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("id__exact", "user__username", "tracking_number")


@admin.register(RefundRequest)
class RefundRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "order", "status", "created_at")
    list_filter = ("status",)

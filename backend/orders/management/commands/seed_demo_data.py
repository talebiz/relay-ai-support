"""
Seed a small demo dataset: ~10 customers, 10 products, 10 orders,
one refund request, and one admin (support agent) user named `admin`.

Multi-tenancy still matters here: each customer owns their own order,
and only the `admin` user (role=SUPPORT_AGENT) can see the full list.
That is the boundary orders/tests/test_authorization.py pins down.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Role, User
from orders.models import Order, OrderStatus, Product, RefundRequest, RefundStatus


CUSTOMERS = [
    ("sara", "sara@example.com"),
    ("arash", "arash@example.com"),
    ("nazanin", "nazanin@example.com"),
    ("reza", "reza@example.com"),
    ("ali", "ali@example.com"),
    ("amir", "amir@example.com"),
    ("zahra", "zahra@example.com"),
    ("hossein", "hossein@example.com"),
    ("mina", "mina@example.com"),
    ("mahdi", "mahdi@example.com"),
]

PRODUCTS = [
    ("HP-001", "Wireless Headphones", "Over-ear, active noise cancelling.", 12999),
    ("KB-042", "Mechanical Keyboard", "Hot-swappable, 75% layout.", 8999),
    ("MS-007", "Ergonomic Mouse", "Vertical grip, 6 buttons.", 4599),
    ("MN-027", "27\" 4K Monitor", "IPS panel, 60Hz, USB-C.", 34999),
    ("WC-100", "1080p Webcam", "Auto-focus, dual microphone.", 5999),
    ("SP-020", "Bluetooth Speaker", "Portable, 12h battery.", 6499),
    ("CH-050", "Fast USB-C Charger", "65W GaN, 2 ports.", 2999),
    ("BG-011", "Laptop Backpack", "Water-resistant, 15\" sleeve.", 7999),
    ("ST-003", "Adjustable Laptop Stand", "Aluminum, 6 heights.", 3999),
    ("HD-256", "External SSD 1TB", "USB 3.2, 1050MB/s read.", 10999),
]

# One order per customer, cycled through the product catalog.
ORDER_STATUSES = [
    OrderStatus.PENDING,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
    OrderStatus.SHIPPED,
    OrderStatus.PROCESSING,
    OrderStatus.DELIVERED,
    OrderStatus.SHIPPED,
    OrderStatus.CANCELLED,
    OrderStatus.PENDING,
]


class Command(BaseCommand):
    help = "Seed demo users, products, orders, and one refund request."

    @transaction.atomic
    def handle(self, *args, **options):
        # --- admin (support agent) ---
        admin, _ = User.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@relay.example",
                "role": Role.SUPPORT_AGENT,
                "is_staff": True,
            },
        )
        admin.role = Role.SUPPORT_AGENT
        admin.is_staff = True
        admin.set_password("password123")
        admin.save()

        # --- customers ---
        customers = []
        for username, email in CUSTOMERS:
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={"email": email, "role": Role.CUSTOMER},
            )
            user.set_password("password123")
            user.save()
            customers.append(user)

        # --- products ---
        products = []
        for sku, name, description, price_cents in PRODUCTS:
            product, _ = Product.objects.get_or_create(
                sku=sku,
                defaults={
                    "name": name,
                    "description": description,
                    "price_cents": price_cents,
                },
            )
            products.append(product)

        # --- one order per customer ---
        # Reruns are safe: we skip creation when the customer already has
        # any order for the chosen product, instead of get_or_create,
        # which raises MultipleObjectsReturned if a previous seed left
        # more than one row.
        orders = []
        for i, customer in enumerate(customers):
            product = products[i % len(products)]
            status = ORDER_STATUSES[i % len(ORDER_STATUSES)]
            tracking = f"TRK-{customer.username.upper()}-{i + 1:03d}" if status in {
                OrderStatus.SHIPPED,
                OrderStatus.DELIVERED,
            } else ""
            order = Order.objects.filter(user=customer, product=product).first()
            if order is None:
                order = Order.objects.create(
                    user=customer,
                    product=product,
                    quantity=(i % 3) + 1,
                    status=status,
                    tracking_number=tracking,
                )
            orders.append(order)

        # --- one refund request, so the refund flow has data to show ---
        if not RefundRequest.objects.filter(order=orders[0]).exists():
            RefundRequest.objects.create(
                user=orders[0].user,
                order=orders[0],
                reason="Right earcup stopped working after a week.",
                status=RefundStatus.REQUESTED,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded 1 admin (admin / password123), "
                f"{len(customers)} customers, {len(products)} products, "
                f"{len(orders)} orders, 1 refund request."
            )
        )

"""
Seed a small, deliberately multi-tenant demo dataset.

Two ordinary customers (alice, bob) each get their own orders and
refund requests, plus one support_agent user. This is the fixture used
manually (and by tests) to prove the authorization boundary actually
holds: logging in as alice and calling GET /api/orders/ must NEVER
return bob's rows, and vice versa; the support agent must see both.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Role, User
from orders.models import Order, OrderStatus, Product, RefundRequest, RefundStatus


class Command(BaseCommand):
    help = "Seed demo users, products, orders, and refund requests."

    @transaction.atomic
    def handle(self, *args, **options):
        alice, _ = User.objects.get_or_create(
            username="alice", defaults={"email": "alice@example.com", "role": Role.CUSTOMER}
        )
        alice.set_password("password123")
        alice.save()

        bob, _ = User.objects.get_or_create(
            username="bob", defaults={"email": "bob@example.com", "role": Role.CUSTOMER}
        )
        bob.set_password("password123")
        bob.save()

        agent, _ = User.objects.get_or_create(
            username="support_sara",
            defaults={"email": "sara@relay.example", "role": Role.SUPPORT_AGENT},
        )
        agent.set_password("password123")
        agent.save()

        headphones, _ = Product.objects.get_or_create(
            sku="HP-001",
            defaults={
                "name": "Wireless Headphones",
                "description": "Over-ear, active noise cancelling.",
                "price_cents": 12999,
            },
        )
        keyboard, _ = Product.objects.get_or_create(
            sku="KB-042",
            defaults={
                "name": "Mechanical Keyboard",
                "description": "Hot-swappable, 75% layout.",
                "price_cents": 8999,
            },
        )

        alice_order, _ = Order.objects.get_or_create(
            user=alice,
            product=headphones,
            defaults={
                "quantity": 1,
                "status": OrderStatus.SHIPPED,
                "tracking_number": "TRK-ALICE-001",
            },
        )
        RefundRequest.objects.get_or_create(
            user=alice,
            order=alice_order,
            defaults={
                "reason": "Right earcup stopped working after a week.",
                "status": RefundStatus.REQUESTED,
            },
        )

        Order.objects.get_or_create(
            user=bob,
            product=keyboard,
            defaults={
                "quantity": 2,
                "status": OrderStatus.PROCESSING,
                "tracking_number": "",
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded users alice/bob/support_sara (password: password123), "
                "2 products, orders, and a refund request."
            )
        )

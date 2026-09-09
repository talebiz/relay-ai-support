"""
Custom user model for Relay.

Why a custom user model at all (instead of django.contrib.auth.User):
Section 2 of the project brief ("bonus" list) mentions role-based access
later (e.g. a "support agent" role allowed to view other users' orders).
Swapping the user model after migrations exist is painful in Django, so
we start with a custom model now even though today it only adds a
`role` field. This costs nothing and avoids a rewrite later.

Authorization principle for this whole codebase (see also orders/models.py
and orders/permissions.py): a customer's role or identity is NEVER decided
by an LLM prompt. It is decided here, in ordinary Python/ORM code, exactly
like it would be in a system with no AI in it at all. The agent (in the
ai-worker service) never receives a "trust me, I'm user 42" instruction
from its own text generation — every tool call it makes is executed by
Django code that independently re-derives the authenticated user from the
JWT/session, not from anything the model said.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    SUPPORT_AGENT = "support_agent", "Support Agent"


class User(AbstractUser):
    """
    Drop-in replacement for Django's default User.

    `role` is authorization-relevant data. It is read by permission
    classes (accounts/permissions.py) and by queryset filtering in the
    orders app - never by anything that constructs a prompt for the LLM.
    """

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CUSTOMER,
        help_text="Drives authorization decisions in code (permissions "
        "and queryset filtering), never passed to the LLM as an "
        "instruction to follow.",
    )

    def __str__(self):
        return self.username

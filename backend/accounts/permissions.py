"""
Shared DRF permission classes.

These are the single source of truth for "who is allowed to see what".
Views use them; the AI worker's tools call the same underlying queryset
helpers (see orders/services.py) so there is exactly one authorization
path in the whole system, whether a human clicks a button in React or
the agent calls a tool on the human's behalf.
"""

from rest_framework.permissions import BasePermission

from accounts.models import Role


class IsSupportAgent(BasePermission):
    """
    Allows access only to users with the support_agent role.

    Used for the (bonus) "support agent can view other users' orders"
    capability. Deliberately a separate, explicit permission class
    rather than a flag checked ad-hoc in view bodies, so every place
    that needs elevated access is grep-able.
    """

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == Role.SUPPORT_AGENT
        )


class IsOwnerOrSupportAgent(BasePermission):
    """
    Object-level permission: the request's user must own the object
    (via `obj.user_id`) OR be a support agent.

    This is intentionally boring, ordinary Django code. Authorization
    never depends on anything an LLM decided; it depends only on
    `request.user`, which DRF populates from the verified JWT before
    any view or tool code runs.
    """

    def has_object_permission(self, request, view, obj):
        if request.user.role == Role.SUPPORT_AGENT:
            return True
        return getattr(obj, "user_id", None) == request.user.id

from rest_framework import serializers

from accounts.models import User


class MeSerializer(serializers.ModelSerializer):
    """Read-only "who am I" payload returned to the frontend after login."""

    class Meta:
        model = User
        fields = ["id", "username", "email", "role"]
        read_only_fields = fields

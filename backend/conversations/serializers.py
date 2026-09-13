from rest_framework import serializers

from conversations.models import Conversation, Message


class ConversationSerializer(serializers.ModelSerializer):
    order_id = serializers.UUIDField(source="order.id", read_only=True)

    class Meta:
        model = Conversation
        fields = ["id", "order_id", "correlation_id", "created_at"]
        read_only_fields = fields


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ["id", "role", "content", "tool_calls_json", "created_at"]
        read_only_fields = fields

"""
Entry point for the AI worker process.

This process is intentionally separate from the Django web process
(see project doc, section 3): response generation must never block
a web worker. It connects to RabbitMQ, consumes "command" messages
(e.g. "generate a reply for this user message"), and will eventually
run the LangGraph agent and publish "event" messages back out
(e.g. streaming deltas, tool-call results) for the SSE view to pick up.

For now this is a skeleton: it proves the container can connect to
RabbitMQ and consume messages. The agent graph, tool definitions,
retries, and idempotency handling are added in later steps.
"""

import os
import time

import pika
from dotenv import load_dotenv

load_dotenv()

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")

# Queue name for incoming commands from the Django backend.
COMMAND_QUEUE = os.getenv("COMMAND_QUEUE", "agent_commands")


def connect_with_retry(max_attempts: int = 10, delay_seconds: float = 3.0) -> pika.BlockingConnection:
    """
    Retry connecting to RabbitMQ with a fixed delay.

    RabbitMQ may still be starting up when this container starts
    (depends_on does not mean "ready", only "started" - see README
    for the healthcheck discussion). This retry loop covers that gap
    during local development; a production setup would use a proper
    healthcheck-based wait instead.
    """
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
    )

    for attempt in range(1, max_attempts + 1):
        try:
            return pika.BlockingConnection(parameters)
        except pika.exceptions.AMQPConnectionError:
            print(f"[worker] RabbitMQ not ready (attempt {attempt}/{max_attempts}), retrying...")
            time.sleep(delay_seconds)

    raise RuntimeError("Could not connect to RabbitMQ after multiple attempts")


def handle_command(channel, method, properties, body):
    """
    Placeholder command handler.

    TODO (next steps):
    - Parse the versioned message envelope (event_id, correlation_id, timestamp).
    - Run the LangGraph agent for this command.
    - Publish progress/result events back to RabbitMQ for the SSE view.
    - Manual ack only after successful processing (see reliability requirements).
    - Apply idempotency checks against the database before reprocessing.
    """
    print(f"[worker] Received command: {body!r}")

    # Manual acknowledgment: only ack after the message is fully handled.
    # This is required for at-least-once delivery / retry semantics.
    channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    connection = connect_with_retry()
    channel = connection.channel()

    # Durable queue so messages survive a RabbitMQ restart.
    channel.queue_declare(queue=COMMAND_QUEUE, durable=True)

    # Fair dispatch: don't hand a new message to this worker until
    # the previous one has been acknowledged.
    channel.basic_qos(prefetch_count=1)

    channel.basic_consume(queue=COMMAND_QUEUE, on_message_callback=handle_command)

    print(f"[worker] Waiting for messages on queue '{COMMAND_QUEUE}'. To exit press CTRL+C")
    channel.start_consuming()


if __name__ == "__main__":
    main()

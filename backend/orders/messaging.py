"""RabbitMQ topology and publisher for order events.

Durable topology + publisher confirms + persistent messages give at-least-once
delivery with no broker-side loss. Paired with the OrderOutbox relay, an event
that cannot be confirmed stays pending and is retried.

Consumer failure handling:
  - poison message (bad payload) -> dead-lettered to the DLQ;
  - transient / out-of-order      -> delayed retry via a TTL queue, then DLQ after
    a bounded number of attempts.
"""

import json

import pika
from django.conf import settings
from pika.exceptions import (
    AMQPConnectionError,
    AMQPError,
    ChannelClosedByBroker,
    UnroutableError,
)

EXCHANGE = "orders"
DLX = "orders.dlx"
OPS_QUEUE = "orders.ops"
LEGACY_PROJECTION_KEYS = ("order.created", "order.status_changed")
OPS_DLQ = "orders.ops.dlq"

RETRY_EXCHANGE = "orders.retry"
RETRY_QUEUE = "orders.ops.retry"
RETRY_TTL_MS = 5000
MAX_RETRIES = 5


def connect() -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))


def declare_topology(channel: "pika.channel.Channel") -> None:
    # all the relay needs: every consumer declares and binds its own queues, so no queue collects events nobody reads
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)


def declare_legacy_topology(channel: "pika.channel.Channel") -> None:
    # the in-storefront projection's queues, declared by that consumer alone; production does not run it, and retire_legacy_queue removes them from a broker
    declare_topology(channel)
    channel.exchange_declare(exchange=DLX, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=RETRY_EXCHANGE, exchange_type="topic", durable=True)

    # final dead-letter queue for poison / exhausted-retry messages
    channel.queue_declare(queue=OPS_DLQ, durable=True)
    channel.queue_bind(queue=OPS_DLQ, exchange=DLX, routing_key="#")

    # delay queue: messages sit for RETRY_TTL_MS, then dead-letter back to the
    # main exchange (original routing key preserved) -> re-delivered to OPS_QUEUE
    channel.queue_declare(
        queue=RETRY_QUEUE,
        durable=True,
        arguments={"x-message-ttl": RETRY_TTL_MS, "x-dead-letter-exchange": EXCHANGE},
    )
    channel.queue_bind(queue=RETRY_QUEUE, exchange=RETRY_EXCHANGE, routing_key="order.*")

    # main ops queue; poison messages nacked here dead-letter straight to the DLQ
    channel.queue_declare(queue=OPS_QUEUE, durable=True, arguments={"x-dead-letter-exchange": DLX})

    for key in LEGACY_PROJECTION_KEYS:
        channel.queue_bind(queue=OPS_QUEUE, exchange=EXCHANGE, routing_key=key)


def converge_legacy_binding(channel) -> None:
    channel.queue_unbind(queue=OPS_QUEUE, exchange=EXCHANGE, routing_key="order.*")


class Publisher:
    """one broker connection for the life of a relay process

    the topology is declared when a channel is opened, not per message. a publish is retried once
    after rebuilding the channel, so a topology that disappeared under a running relay is repaired
    without paying for a declaration on every message. anything still unconfirmed is raised, and
    the caller keeps the outbox row pending
    """

    # the connection also dies on its own: nothing services its heartbeats while the outbox is
    # empty. the retry covers that too, and adds no duplicate risk the pending row did not
    # already carry, since an unconfirmed publish is republished on the next drain either way
    REBUILD_ON = (UnroutableError, ChannelClosedByBroker, AMQPConnectionError)

    def __init__(self) -> None:
        self._conn = None
        self._chan = None

    def publish(self, routing_key: str, event_type: str, payload: dict, headers: dict) -> None:
        try:
            publish_message(self._channel(), EXCHANGE, routing_key, event_type, payload, headers)
        except self.REBUILD_ON:
            self.close()
            publish_message(self._channel(), EXCHANGE, routing_key, event_type, payload, headers)

    def close(self) -> None:
        self._chan = None
        if self._conn is not None:
            try:
                self._conn.close()
            except AMQPError:
                pass
        self._conn = None

    def _channel(self):
        if self._chan is not None and self._chan.is_open:
            return self._chan
        if self._conn is None or not self._conn.is_open:
            self._conn = connect()
        channel = self._conn.channel()
        declare_topology(channel)
        channel.confirm_delivery()
        self._chan = channel
        return channel


def publish_message(channel, exchange, routing_key, event_type, payload, headers) -> None:
    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=json.dumps(payload, ensure_ascii=False).encode(),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # persistent
            type=event_type,
            message_id=str(headers.get("event_id", "")),
            correlation_id=str(headers.get("correlation_id", "")),
            headers=headers,
        ),
        mandatory=True,
    )

"""
the bridge's retry publish against a real broker

skipped unless BRIDGE_MQ_URL_FOR_TRANSPORT is set; it declares its own throwaway queues and the bridge retry exchange, so point it only at a broker no bridge is attached to
"""

import os
import time
import uuid

import pika
import pytest
from pika.exceptions import AMQPConnectionError

from operations.management.commands.bridge_storefront_events import BRIDGE_RETRY_EXCHANGE, Command

URL = os.environ.get("BRIDGE_MQ_URL_FOR_TRANSPORT")

if os.environ.get("REQUIRE_BRIDGE_TRANSPORT_TESTS") == "1" and not URL:
    raise RuntimeError(
        "REQUIRE_BRIDGE_TRANSPORT_TESTS is set but BRIDGE_MQ_URL_FOR_TRANSPORT is missing"
    )

pytestmark = pytest.mark.skipif(not URL, reason="BRIDGE_MQ_URL_FOR_TRANSPORT not configured")


def _connect():
    deadline = time.monotonic() + 60
    while True:
        try:
            return pika.BlockingConnection(pika.URLParameters(URL))
        except AMQPConnectionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def test_a_retry_nothing_can_receive_keeps_the_original_in_the_dead_letter_queue():
    suffix = uuid.uuid4().hex[:8]
    source, dead, dlx = (
        f"bridge.live.source.{suffix}",
        f"bridge.live.dlq.{suffix}",
        f"bridge.live.dlx.{suffix}",
    )
    connection = _connect()
    admin = connection.channel()
    admin.exchange_declare(exchange=dlx, exchange_type="fanout", durable=True)
    admin.queue_declare(queue=dead, durable=True)
    admin.queue_bind(queue=dead, exchange=dlx)
    admin.queue_declare(queue=source, durable=True, arguments={"x-dead-letter-exchange": dlx})
    # the retry exchange with nothing bound: the broker can only return the copy
    admin.exchange_delete(exchange=BRIDGE_RETRY_EXCHANGE)
    admin.exchange_declare(exchange=BRIDGE_RETRY_EXCHANGE, exchange_type="topic", durable=True)
    admin.basic_publish(
        exchange="",
        routing_key=source,
        body=b'{"order_id": 1}',
        properties=pika.BasicProperties(type="order.created", headers={}),
    )
    try:
        channel = connection.channel()
        channel.confirm_delivery()
        method, properties, body = channel.basic_get(queue=source, auto_ack=False)
        assert method is not None

        Command()._retry(channel, method, properties, body)
        channel.close()

        assert admin.queue_declare(queue=source, passive=True).method.message_count == 0
        assert admin.queue_declare(queue=dead, passive=True).method.message_count == 1
    finally:
        for queue in (source, dead):
            admin.queue_delete(queue=queue)
        admin.exchange_delete(exchange=dlx)
        admin.exchange_delete(exchange=BRIDGE_RETRY_EXCHANGE)
        connection.close()

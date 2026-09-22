"""
retirement of the legacy orders.ops queue against a real broker

skipped unless COMMANDS_MQ_URL_FOR_TRANSPORT is set; destructive for orders.ops and its retry and dead letter queues, so point it only at a throwaway broker
"""

import os
import time

import pika
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pika.exceptions import AMQPConnectionError, ChannelClosedByBroker, UnroutableError

from orders import messaging

URL = os.environ.get("COMMANDS_MQ_URL_FOR_TRANSPORT")

pytestmark = pytest.mark.skipif(not URL, reason="COMMANDS_MQ_URL_FOR_TRANSPORT not configured")


def _connect():
    deadline = time.monotonic() + 60
    while True:
        try:
            return pika.BlockingConnection(pika.URLParameters(URL))
        except AMQPConnectionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


@pytest.fixture
def broker(settings):
    settings.RABBITMQ_URL = URL
    connection = _connect()
    channel = connection.channel()
    for queue in (messaging.OPS_QUEUE, messaging.RETRY_QUEUE, messaging.OPS_DLQ):
        channel.queue_delete(queue=queue)
    messaging.declare_legacy_topology(channel)
    channel.confirm_delivery()
    yield channel
    if connection.is_open:
        connection.close()


def _publish(channel, routing_key="order.created"):
    channel.basic_publish(
        exchange=messaging.EXCHANGE,
        routing_key=routing_key,
        body=b"{}",
        properties=pika.BasicProperties(delivery_mode=2),
        mandatory=True,
    )


def _exists(queue) -> bool:
    connection = _connect()
    try:
        connection.channel().queue_declare(queue=queue, passive=True)
        return True
    except ChannelClosedByBroker as exc:
        assert exc.reply_code == 404
        return False
    finally:
        if connection.is_open:
            connection.close()


def test_a_backlog_is_unbound_and_named_but_not_deleted(broker):
    _publish(broker)
    _publish(broker, "order.status_changed")

    with pytest.raises(CommandError, match="orders.ops: 2"):
        call_command("retire_legacy_queue")

    assert _exists(messaging.OPS_QUEUE)
    with pytest.raises(UnroutableError):
        _publish(broker)


def test_an_empty_legacy_queue_is_deleted_and_a_second_run_finds_nothing(broker):
    call_command("retire_legacy_queue")

    for queue in (messaging.OPS_QUEUE, messaging.RETRY_QUEUE, messaging.OPS_DLQ):
        assert not _exists(queue)
    call_command("retire_legacy_queue")


def test_the_backlog_goes_only_when_it_is_discarded_explicitly(broker):
    _publish(broker)
    with pytest.raises(CommandError):
        call_command("retire_legacy_queue")

    call_command("retire_legacy_queue", "--discard-backlog")

    assert not _exists(messaging.OPS_QUEUE)

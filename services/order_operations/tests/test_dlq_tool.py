"""operator handling of the dead-letter queues: a listing consumes nothing, a replay goes back where it fell out"""

import json
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from operations import messaging
from operations.management.commands import bridge_storefront_events as bridge
from operations.management.commands import dlq as dlq_command


class FakeChannel:
    """models one queue of deliveries plus the publishes made against it"""

    def __init__(self, messages, message_type=None):
        self.queue = list(messages)
        self.message_type = message_type
        self.unacked: dict[int, tuple] = {}
        self.published: list[dict] = []
        self.acked: list[int] = []
        self.requeued: list[int] = []
        self.read_from: set[str] = set()
        self.publish_error = None
        self._tag = 0

    def basic_get(self, queue, auto_ack=False):
        self.read_from.add(queue)
        if not self.queue:
            return None, None, None
        self._tag += 1
        routing_key, headers, body = self.queue.pop(0)
        method = SimpleNamespace(delivery_tag=self._tag, routing_key=routing_key, redelivered=False)
        properties = SimpleNamespace(
            headers=dict(headers), message_id=f"m{self._tag}", correlation_id=None,
            content_type="application/json", type=self.message_type,
        )
        self.unacked[self._tag] = (routing_key, headers, body)
        return method, properties, body

    def basic_publish(self, exchange, routing_key, body, properties=None, mandatory=False):
        if self.publish_error:
            raise self.publish_error
        self.published.append({
            "exchange": exchange, "routing_key": routing_key, "body": body,
            "headers": dict((properties.headers or {}) if properties else {}),
            "type": properties.type if properties else None,
        })

    def basic_ack(self, delivery_tag):
        self.acked.append(delivery_tag)
        self.unacked.pop(delivery_tag, None)

    def basic_nack(self, delivery_tag, requeue=False):
        if requeue:
            self.requeued.append(delivery_tag)
            self.queue.append(self.unacked.pop(delivery_tag))
        else:
            self.unacked.pop(delivery_tag, None)

    def confirm_delivery(self):
        pass

    def close(self):
        pass


def _messages(count=3, key="orders.order.created.v1"):
    return [
        (key, {"x-retries": 5, "x-death": [{"reason": "rejected"}]},
         json.dumps({"event_type": key, "n": i}).encode())
        for i in range(count)
    ]


@pytest.fixture
def channel(monkeypatch):
    chan = FakeChannel(_messages())
    connection = SimpleNamespace(channel=lambda: chan, close=lambda: None, is_open=True)
    monkeypatch.setattr(dlq_command.messaging, "connect", lambda: connection)
    return chan


def test_listing_leaves_every_message_in_place(channel, capsys):
    call_command("dlq", "--list")

    assert len(channel.queue) == 3
    assert channel.acked == []
    out = capsys.readouterr().out
    assert "orders.order.created.v1" in out and "3" in out


def test_replay_publishes_with_the_original_routing_key(channel):
    call_command("dlq", "--replay", "2")

    assert [p["routing_key"] for p in channel.published] == ["orders.order.created.v1"] * 2
    assert {p["exchange"] for p in channel.published} == {messaging.EXCHANGE}
    assert len(channel.acked) == 2
    assert len(channel.queue) == 1


def test_replay_clears_the_spent_retry_budget_and_records_who_did_it(channel):
    call_command("dlq", "--replay", "1", "--operator", "ops-7")

    headers = channel.published[0]["headers"]
    assert "x-retries" not in headers
    assert headers["x-replayed-by"] == "ops-7"
    assert headers["x-replayed-at"]


def test_a_message_is_never_acked_before_its_publish_is_confirmed(channel):
    channel.publish_error = RuntimeError("broker refused")

    with pytest.raises(CommandError):
        call_command("dlq", "--replay", "3")

    assert channel.acked == []
    assert len(channel.queue) == 3, "the message must stay in the dead-letter queue"


def test_replay_stops_at_the_requested_count(channel):
    call_command("dlq", "--replay", "1")

    assert len(channel.published) == 1
    assert len(channel.queue) == 2


def test_an_empty_queue_is_not_an_error(monkeypatch):
    chan = FakeChannel([])
    monkeypatch.setattr(dlq_command.messaging, "connect",
                        lambda: SimpleNamespace(channel=lambda: chan, close=lambda: None, is_open=True))

    call_command("dlq", "--replay", "5")

    assert chan.published == []


def test_dropping_needs_an_explicit_confirmation_and_a_reason(channel):
    with pytest.raises(CommandError):
        call_command("dlq", "--drop", "1")
    with pytest.raises(CommandError):
        call_command("dlq", "--drop", "1", "--yes")

    assert len(channel.queue) == 3, "nothing may be discarded without both"


def test_a_confirmed_drop_discards_exactly_the_requested_count(channel, caplog):
    call_command("dlq", "--drop", "2", "--yes", "--reason", "contract changed, unprocessable")

    assert len(channel.acked) == 2
    assert len(channel.queue) == 1
    assert channel.published == []
    assert any("dlq message discarded" in r.message for r in caplog.records)


def test_replay_and_drop_are_mutually_exclusive(channel):
    with pytest.raises(CommandError):
        call_command("dlq", "--replay", "1", "--drop", "1", "--yes", "--reason", "x")

    assert len(channel.queue) == 3


@pytest.fixture
def bridge_channel(monkeypatch):
    spent = {
        bridge.RETRY_HEADER: 5,
        "x-death": [{"reason": "rejected"}],
        "occurred_at": "2026-10-31T10:00:00+00:00",
    }
    chan = FakeChannel(
        [("order.status_changed", spent, b'{"order_id": 7}')], message_type="order.status_changed"
    )
    connection = SimpleNamespace(channel=lambda: chan, close=lambda: None, is_open=True)
    monkeypatch.setattr(dlq_command, "_bridge_connect", lambda: connection)
    monkeypatch.setattr(
        dlq_command.messaging,
        "connect",
        lambda: pytest.fail("the bridge queue is not on the operations vhost"),
    )
    return chan


def test_the_bridge_queue_is_read_with_the_bridge_connection(bridge_channel, capsys):
    call_command("dlq", "--queue", "bridge", "--list")

    assert bridge_channel.read_from == {bridge.BRIDGE_DLQ}
    assert len(bridge_channel.queue) == 1
    out = capsys.readouterr().out
    assert bridge.BRIDGE_DLQ in out and "order.status_changed" in out


def test_a_bridge_replay_goes_back_through_the_requeue_exchange_as_the_same_event(bridge_channel):
    call_command("dlq", "--queue", "bridge", "--replay", "1", "--operator", "ops-7")

    published = bridge_channel.published[0]
    assert published["exchange"] == bridge.BRIDGE_REQUEUE_EXCHANGE
    assert published["routing_key"] == "order.status_changed"
    assert published["type"] == "order.status_changed"
    assert bridge.RETRY_HEADER not in published["headers"] and "x-death" not in published["headers"]
    assert published["headers"]["occurred_at"] == "2026-10-31T10:00:00+00:00"
    assert len(bridge_channel.acked) == 1

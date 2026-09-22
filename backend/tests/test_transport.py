import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone

from ops import service
from ops.management.commands.run_ops_consumer import Command as OpsConsumer
from orders import messaging
from orders.management.commands.publish_outbox import Command as Relay
from orders.management.commands.publish_outbox import _backoff_seconds
from orders.models import OrderOutbox

pytestmark = pytest.mark.django_db


def _msg(event_type="order.created", schema=1, retries=0, order_id=1):
    method = SimpleNamespace(delivery_tag=1, routing_key=event_type)
    event_id = str(uuid.uuid4())
    headers = {
        "schema_version": schema,
        "event_id": event_id,
        "correlation_id": str(uuid.uuid4()),
        "aggregate_version": 1,
        "x-retries": retries,
    }
    props = SimpleNamespace(type=event_type, message_id=event_id, headers=headers)
    body = json.dumps({"order_id": order_id, "status": "created", "items": []}).encode()
    return method, props, body


def test_bad_schema_goes_to_dlq():
    channel = Mock()
    OpsConsumer()._on_message(channel, *_msg(schema=99))
    channel.basic_nack.assert_called_once_with(1, requeue=False)
    channel.basic_ack.assert_not_called()


def test_unknown_event_type_goes_to_dlq():
    channel = Mock()
    OpsConsumer()._on_message(channel, *_msg(event_type="weird.event"))
    channel.basic_nack.assert_called_once_with(1, requeue=False)


def test_missing_order_id_goes_to_dlq():
    channel = Mock()
    method, props, _ = _msg()  # valid envelope, but body lacks order_id
    OpsConsumer()._on_message(channel, method, props, b'{"status":"created"}')
    channel.basic_nack.assert_called_once_with(1, requeue=False)


def test_missing_correlation_id_goes_to_dlq():
    channel = Mock()
    method, props, body = _msg()
    del props.headers["correlation_id"]
    OpsConsumer()._on_message(channel, method, props, body)
    channel.basic_nack.assert_called_once_with(1, requeue=False)


def test_aggregate_version_below_one_goes_to_dlq():
    channel = Mock()
    method, props, body = _msg()
    props.headers["aggregate_version"] = 0
    OpsConsumer()._on_message(channel, method, props, body)
    channel.basic_nack.assert_called_once_with(1, requeue=False)


def test_non_uuid_event_id_goes_to_dlq():
    channel = Mock()
    method, props, body = _msg()
    props.headers["event_id"] = "None"
    props.message_id = "None"
    OpsConsumer()._on_message(channel, method, props, body)
    channel.basic_nack.assert_called_once_with(1, requeue=False)


def test_malformed_retries_header_does_not_crash(monkeypatch):
    monkeypatch.setattr(service, "apply_event", lambda **k: True)
    channel = Mock()
    method, props, body = _msg()
    props.headers["x-retries"] = "not-a-number"
    OpsConsumer()._on_message(channel, method, props, body)
    channel.basic_ack.assert_called_once_with(1)


def test_out_of_order_publishes_confirmed_retry_then_acks(monkeypatch):
    monkeypatch.setattr(service, "apply_event", Mock(side_effect=service.OutOfOrder("x")))
    seen = {}
    monkeypatch.setattr(
        messaging,
        "publish_message",
        lambda ch, ex, rk, et, pl, hd: seen.update(exchange=ex, retries=hd["x-retries"]),
    )
    channel = Mock()
    OpsConsumer()._on_message(channel, *_msg(retries=0))
    assert seen == {"exchange": messaging.RETRY_EXCHANGE, "retries": 1}
    channel.basic_ack.assert_called_once_with(1)


def test_retry_publish_failure_requeues_original(monkeypatch):
    monkeypatch.setattr(service, "apply_event", Mock(side_effect=service.OutOfOrder("x")))
    monkeypatch.setattr(messaging, "publish_message", Mock(side_effect=Exception("broker down")))
    channel = Mock()
    OpsConsumer()._on_message(channel, *_msg(retries=0))
    channel.basic_nack.assert_called_once_with(1, requeue=True)  # original not lost
    channel.basic_ack.assert_not_called()


def test_exhausted_retries_go_to_dlq(monkeypatch):
    monkeypatch.setattr(service, "apply_event", Mock(side_effect=service.OutOfOrder("x")))
    channel = Mock()
    OpsConsumer()._on_message(channel, *_msg(retries=messaging.MAX_RETRIES))
    channel.basic_nack.assert_called_once_with(1, requeue=False)  # DLQ


# leased outbox relay
def _outbox(aggregate_id="1", **kw):
    return OrderOutbox.objects.create(
        aggregate_type="order",
        aggregate_id=aggregate_id,
        event_type="order.created",
        routing_key="order.created",
        payload={"order_id": 1},
        **kw,
    )


def test_relay_publish_failure_backs_off_and_stays_pending(monkeypatch):
    row = _outbox()
    relay = Relay()
    [claimed] = relay._claim("w1")
    monkeypatch.setattr(relay._publisher, "publish", Mock(side_effect=Exception("broker down")))
    relay._publish_one("w1", claimed)
    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert "Exception" in row.last_error, "the failure is recorded by shape, not by its text"


def test_relay_success_marks_published(monkeypatch):
    row = _outbox()
    relay = Relay()
    [claimed] = relay._claim("w1")
    monkeypatch.setattr(relay._publisher, "publish", Mock())
    relay._publish_one("w1", claimed)
    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PUBLISHED
    assert row.published_at is not None


def test_backoff_grows_with_attempts():
    assert _backoff_seconds(5) > _backoff_seconds(1)


def test_claim_leases_rows_so_a_second_worker_skips_them():
    for n in range(5):
        _outbox(aggregate_id=str(n))
    relay = Relay()
    first = relay._claim("w1")
    assert len(first) == 5
    assert relay._claim("w2") == []  # all leased -> nothing left


def test_expired_lease_is_reclaimed_by_second_worker():
    row = _outbox()
    relay = Relay()
    assert [r.pk for r in relay._claim("w1")] == [row.pk]

    OrderOutbox.objects.filter(pk=row.pk).update(locked_until=timezone.now() - timedelta(seconds=1))

    assert [r.pk for r in relay._claim("w2")] == [row.pk]  # lease expired -> reclaimable
    row.refresh_from_db()
    assert row.locked_by == "w2"


def test_publish_not_committed_when_lease_expired(monkeypatch):
    # a slow worker whose lease already expired must not flip status
    row = _outbox()
    relay = Relay()
    [claimed] = relay._claim("w1")
    OrderOutbox.objects.filter(pk=row.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    monkeypatch.setattr(relay._publisher, "publish", Mock())
    relay._publish_one("w1", claimed)
    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING


@pytest.mark.django_db(transaction=True)
def test_parallel_workers_do_not_double_claim():
    for _ in range(20):
        _outbox()
    relay = Relay()

    def worker(worker_id):
        from django.db import connection

        try:
            return [row.pk for row in relay._claim(worker_id)]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(worker, "w1")
        b = pool.submit(worker, "w2")
        ids_a, ids_b = a.result(), b.result()

    assert set(ids_a).isdisjoint(ids_b)  # skip_locked -> no row claimed twice
    assert len(ids_a) + len(ids_b) <= 20
    OrderOutbox.objects.all().delete()  # transaction=True: clean up explicitly


def test_the_relay_declares_its_exchange_and_no_queue():
    from unittest.mock import MagicMock

    from orders import messaging

    channel = MagicMock()
    messaging.declare_topology(channel)

    channel.exchange_declare.assert_called_once_with(
        exchange=messaging.EXCHANGE, exchange_type="topic", durable=True
    )
    channel.queue_declare.assert_not_called()
    channel.queue_bind.assert_not_called()


def test_relay_startup_leaves_the_legacy_queue_alone(monkeypatch):
    from unittest.mock import MagicMock

    from orders import messaging
    from orders.management.commands.publish_outbox import Command

    connection = MagicMock()
    monkeypatch.setattr(messaging, "connect", lambda: connection)
    Command()._converge_topology()

    channel = connection.channel.return_value
    channel.queue_declare.assert_not_called()
    channel.queue_bind.assert_not_called()
    channel.queue_unbind.assert_not_called()


def test_legacy_queue_is_bound_only_to_what_it_can_process():
    from unittest.mock import MagicMock

    from orders import messaging

    channel = MagicMock()
    messaging.declare_legacy_topology(channel)

    ops_binds = [
        c.kwargs["routing_key"]
        for c in channel.queue_bind.call_args_list
        if c.kwargs.get("queue") == messaging.OPS_QUEUE and c.kwargs.get("exchange") == messaging.EXCHANGE
    ]
    assert ops_binds == list(messaging.LEGACY_PROJECTION_KEYS)
    assert "order.*" not in ops_binds
    channel.queue_unbind.assert_not_called()


def test_legacy_wildcard_binding_is_converged_away():
    from unittest.mock import MagicMock

    from orders import messaging

    channel = MagicMock()
    messaging.converge_legacy_binding(channel)

    channel.queue_unbind.assert_called_once_with(
        queue=messaging.OPS_QUEUE, exchange=messaging.EXCHANGE, routing_key="order.*"
    )


class _FakeConnection:
    """stands in for a broker connection and records how often channels are opened"""

    def __init__(self, channels: list) -> None:
        self.is_open = True
        self._channels = channels

    def channel(self):
        from unittest.mock import MagicMock

        chan = MagicMock()
        chan.is_open = True
        self._channels.append(chan)
        return chan

    def close(self) -> None:
        self.is_open = False


def _pending_rows(count: int) -> None:
    from orders.models import OrderOutbox

    for i in range(count):
        OrderOutbox.objects.create(
            aggregate_type="order",
            aggregate_id=str(i), event_type="order.created",
            routing_key="order.created", payload={"order_id": i},
        )


def _relay_with(monkeypatch, connect):
    from orders import messaging
    from orders.management.commands.publish_outbox import Command

    monkeypatch.setattr(messaging, "connect", connect)
    return Command()


def test_a_batch_shares_one_broker_connection(db, monkeypatch):
    opened, channels = [], []

    def connect():
        conn = _FakeConnection(channels)
        opened.append(conn)
        return conn

    _pending_rows(3)
    relay = _relay_with(monkeypatch, connect)

    assert relay._drain("w1") == 3
    assert len(opened) == 1, f"opened {len(opened)} connections for 3 messages"


def test_topology_is_not_declared_per_message(db, monkeypatch):
    from orders import messaging

    declared = []
    channels = []
    monkeypatch.setattr(messaging, "connect", lambda: _FakeConnection(channels))
    monkeypatch.setattr(messaging, "declare_topology", lambda channel: declared.append(channel))

    _pending_rows(3)
    _relay_with(monkeypatch, lambda: _FakeConnection(channels))._drain("w1")

    assert len(declared) == 1, f"declared the topology {len(declared)} times for 3 messages"


def test_a_vanished_topology_is_rebuilt_and_the_message_retried(db, monkeypatch):
    from pika.exceptions import UnroutableError

    from orders import messaging
    from orders.models import OrderOutbox

    channels = []
    monkeypatch.setattr(messaging, "connect", lambda: _FakeConnection(channels))
    calls = {"n": 0}

    def flaky(channel, exchange, routing_key, event_type, payload, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnroutableError([])

    monkeypatch.setattr(messaging, "publish_message", flaky)
    _pending_rows(1)

    _relay_with(monkeypatch, lambda: _FakeConnection(channels))._drain("w1")

    assert calls["n"] == 2, "the publisher did not rebuild its channel and retry"
    assert OrderOutbox.objects.get().status == OrderOutbox.Status.PUBLISHED


def test_a_publish_that_never_confirms_leaves_the_row_pending(db, monkeypatch):
    from pika.exceptions import UnroutableError

    from orders import messaging
    from orders.models import OrderOutbox

    channels = []
    monkeypatch.setattr(messaging, "connect", lambda: _FakeConnection(channels))

    def always_unroutable(*args, **kwargs):
        raise UnroutableError([])

    monkeypatch.setattr(messaging, "publish_message", always_unroutable)
    _pending_rows(1)

    assert _relay_with(monkeypatch, lambda: _FakeConnection(channels))._drain("w1") == 1

    row = OrderOutbox.objects.get()
    assert row.status == OrderOutbox.Status.PENDING
    assert row.attempts == 1 and row.next_attempt_at is not None


def test_a_superseded_worker_with_the_same_identity_cannot_publish(monkeypatch):
    row = _outbox()
    holder = Relay()
    [stale] = holder._claim("relay:1")
    OrderOutbox.objects.filter(pk=row.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    # the same identity string: in a container the pid is 1 for every replica
    assert [r.pk for r in Relay()._claim("relay:1")] == [row.pk]

    monkeypatch.setattr(holder._publisher, "publish", Mock())
    holder._publish_one("relay:1", stale)

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING, "a superseded worker completed another lease"


def test_a_superseded_worker_cannot_reschedule_another_lease(monkeypatch):
    row = _outbox()
    holder = Relay()
    [stale] = holder._claim("relay:1")
    OrderOutbox.objects.filter(pk=row.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    Relay()._claim("relay:1")
    fresh = OrderOutbox.objects.get(pk=row.pk)

    monkeypatch.setattr(holder._publisher, "publish", Mock(side_effect=Exception("broker down")))
    holder._publish_one("relay:1", stale)

    row.refresh_from_db()
    assert row.locked_until == fresh.locked_until, "the current lease was released by a stale worker"
    assert row.attempts == fresh.attempts


def test_an_unclaimed_row_is_never_marked_published(monkeypatch):
    row = _outbox()
    relay = Relay()
    monkeypatch.setattr(relay._publisher, "publish", Mock())

    relay._publish_one("relay:1", row)

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING


def test_a_row_with_a_live_lock_but_no_token_is_never_completed(monkeypatch):
    row = _outbox()
    # the only state where the two fencing checks differ: a live window without a token
    OrderOutbox.objects.filter(pk=row.pk).update(
        locked_by="relay:1", locked_until=timezone.now() + timedelta(seconds=60)
    )
    stale = OrderOutbox.objects.get(pk=row.pk)
    relay = Relay()
    monkeypatch.setattr(relay._publisher, "publish", Mock())

    relay._publish_one("relay:1", stale)

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING


def _outcome_row():
    import uuid as _uuid

    return OrderOutbox.objects.create(
        aggregate_type="order",
        event_type="orders.transition.succeeded.v1",
        routing_key="order.transition_succeeded",
        aggregate_id="42",
        aggregate_version=2,
        causation_id=_uuid.uuid4(),
        payload={"command_id": str(_uuid.uuid4()), "order_id": 42,
                 "from_status": "created", "status": "confirmed"},
    )


def _captured_body(monkeypatch, row):
    sent = {}

    def publish(routing_key, event_type, payload, headers):
        sent["routing_key"] = routing_key
        sent["payload"] = payload

    relay = Relay()
    [claimed] = relay._claim("w1")
    monkeypatch.setattr(relay._publisher, "publish", publish)
    relay._publish_one("w1", claimed)
    return sent


def test_outcome_events_are_published_as_a_contract_envelope(monkeypatch):
    from event_contracts import parse_event

    row = _outcome_row()

    sent = _captured_body(monkeypatch, row)

    envelope, data = parse_event(sent["payload"])
    assert str(envelope.event_id) == str(row.event_id)
    assert str(envelope.correlation_id) == str(row.correlation_id)
    assert str(envelope.causation_id) == str(row.causation_id)
    assert envelope.producer == "storefront"
    assert envelope.aggregate.id == "42" and envelope.aggregate.version == 2
    assert str(data.order_id) == "42"


def test_legacy_events_keep_their_bare_payload(monkeypatch):
    row = _outbox()

    sent = _captured_body(monkeypatch, row)

    assert sent["payload"] == row.payload


def test_the_relay_reports_the_age_of_the_oldest_unsent_event(monkeypatch):
    from prometheus_client import CollectorRegistry, Gauge

    row = _outbox()
    OrderOutbox.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(seconds=120))
    relay = Relay()
    monkeypatch.setattr(relay._publisher, "publish", Mock(side_effect=Exception("broker down")))
    gauge = Gauge("probe_outbox_age_seconds", "probe", registry=CollectorRegistry())

    relay._drain("w1", gauge)

    reported = gauge.collect()[0].samples[0].value
    assert 110 <= reported <= 130, f"the backlog age reported as {reported}"


def test_a_connection_lost_while_idle_is_rebuilt_and_the_message_retried(db, monkeypatch):
    """the relay holds one connection and does no I/O between events

    nothing services its heartbeats while the outbox is empty, so the broker eventually drops it.
    the next event must not pay for that with a failed attempt and a backoff
    """
    from pika.exceptions import StreamLostError

    from orders import messaging
    from orders.models import OrderOutbox

    channels = []
    monkeypatch.setattr(messaging, "connect", lambda: _FakeConnection(channels))
    calls = {"n": 0}

    def dropped_once(channel, exchange, routing_key, event_type, payload, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StreamLostError("Stream connection lost")

    monkeypatch.setattr(messaging, "publish_message", dropped_once)
    _pending_rows(1)

    _relay_with(monkeypatch, lambda: _FakeConnection(channels))._drain("w1")

    assert calls["n"] == 2, "the publisher did not rebuild the connection and retry"
    row = OrderOutbox.objects.get()
    assert row.status == OrderOutbox.Status.PUBLISHED
    assert row.attempts == 1, "a reconnect must not burn an extra attempt"

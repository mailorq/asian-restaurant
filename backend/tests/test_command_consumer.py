import datetime as dt
import json
import types
import uuid

import pytest
from django.db import OperationalError
from event_contracts import OrderTransitionRequestedData

from orders import command_messaging as topology
from orders.management.commands.consume_commands import Command as ConsumerCommand
from orders.models import CommandInbox, Order, OrderOutbox

pytestmark = pytest.mark.django_db


class FakeChannel:
    """records what the consumer does with a delivery instead of talking to a broker"""

    def __init__(self) -> None:
        self.acked: list[int] = []
        self.nacked: list[tuple[int, bool]] = []
        self.published: list[dict] = []

    def basic_ack(self, delivery_tag) -> None:
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue=False) -> None:
        self.nacked.append((delivery_tag, requeue))

    def basic_publish(self, **kwargs) -> None:
        self.published.append(kwargs)


def _method(routing_key=topology.REQUESTED_ROUTING_KEY, delivery_tag=1):
    return types.SimpleNamespace(routing_key=routing_key, delivery_tag=delivery_tag)


def _properties(headers=None):
    return types.SimpleNamespace(
        headers=headers, message_id=str(uuid.uuid4()), correlation_id=str(uuid.uuid4())
    )


def _request(actor, order_id, *, expected="created", target="confirmed", ttl_seconds=30):
    return OrderTransitionRequestedData(
        command_id=uuid.uuid4(),
        actor_id=actor.id,
        actor_authz_version=actor.authz_version,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds),
        order_id=order_id,
        expected_status=expected,
        target_status=target,
        reason="",
    )


def _body(data, *, event_type="orders.transition.requested.v1", event_id=None):
    envelope = {
        "event_id": str(event_id or uuid.uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        "producer": "operations",
        "aggregate": {"type": "order", "id": str(data.order_id), "version": 1},
        "correlation_id": str(uuid.uuid4()),
        "data": json.loads(data.model_dump_json()),
    }
    return json.dumps(envelope).encode()


@pytest.fixture
def order(user, make_product, seed_cart):
    from orders import service as order_service

    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    return order_service.checkout(user, "ул. Пушкина, 12", "cash", f"idem-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def consumer():
    return ConsumerCommand()


def test_applied_command_is_acked_and_moves_the_order(consumer, order, employee_user):
    channel = FakeChannel()

    consumer._on_message(channel, _method(), _properties(), _body(_request(employee_user, order.id)))

    assert channel.acked == [1] and channel.nacked == []
    order.refresh_from_db()
    assert order.status == "confirmed"
    assert OrderOutbox.objects.filter(event_type="orders.transition.succeeded.v1").count() == 1


def test_redelivery_replays_the_stored_outcome_without_a_second_event(consumer, order, employee_user):
    channel = FakeChannel()
    body = _body(_request(employee_user, order.id))

    consumer._on_message(channel, _method(delivery_tag=1), _properties(), body)
    consumer._on_message(channel, _method(delivery_tag=2), _properties(), body)

    assert channel.acked == [1, 2] and channel.nacked == []
    assert CommandInbox.objects.count() == 1
    assert OrderOutbox.objects.filter(event_type="orders.transition.succeeded.v1").count() == 1


def test_rejected_command_is_still_acked(consumer, order, employee_user):
    channel = FakeChannel()
    # the order is at created, so a command expecting delivering is refused, not retried
    body = _body(_request(employee_user, order.id, expected="delivering", target="delivered"))

    consumer._on_message(channel, _method(), _properties(), body)

    assert channel.acked == [1] and channel.nacked == []
    assert OrderOutbox.objects.filter(event_type="orders.transition.rejected.v1").count() == 1


def test_unparsable_body_goes_to_the_dlq(consumer):
    channel = FakeChannel()

    consumer._on_message(channel, _method(), _properties(), b"{not json")

    assert channel.nacked == [(1, False)] and channel.acked == []


def test_foreign_event_type_goes_to_the_dlq(consumer, order, employee_user):
    channel = FakeChannel()
    body = _body(_request(employee_user, order.id), event_type="orders.transition.succeeded.v1")

    consumer._on_message(channel, _method(), _properties(), body)

    assert channel.nacked == [(1, False)]
    assert CommandInbox.objects.count() == 0


def test_database_failure_is_retried_with_an_incremented_header(consumer, order, employee_user, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(
        "orders.management.commands.consume_commands.apply_transition_command",
        lambda *a, **k: (_ for _ in ()).throw(OperationalError("deadlock detected")),
    )

    consumer._on_message(channel, _method(), _properties(), _body(_request(employee_user, order.id)))

    assert len(channel.published) == 1
    republished = channel.published[0]
    assert republished["exchange"] == topology.RETRY_EXCHANGE
    assert republished["routing_key"] == topology.REQUESTED_ROUTING_KEY
    assert republished["properties"].headers["x-retries"] == 1
    assert channel.acked == [1] and channel.nacked == []


def test_dropped_database_connection_is_retried_not_dead_lettered(consumer, order, employee_user, monkeypatch):
    from django.db import InterfaceError

    channel = FakeChannel()
    monkeypatch.setattr(
        "orders.management.commands.consume_commands.apply_transition_command",
        lambda *a, **k: (_ for _ in ()).throw(InterfaceError("connection already closed")),
    )

    consumer._on_message(channel, _method(), _properties(), _body(_request(employee_user, order.id)))

    assert len(channel.published) == 1 and channel.acked == [1]
    assert channel.nacked == []


def test_exhausted_retries_go_to_the_dlq(consumer, order, employee_user, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(
        "orders.management.commands.consume_commands.apply_transition_command",
        lambda *a, **k: (_ for _ in ()).throw(OperationalError("deadlock detected")),
    )
    properties = _properties({"x-retries": topology.MAX_RETRIES})

    consumer._on_message(channel, _method(), properties, _body(_request(employee_user, order.id)))

    assert channel.nacked == [(1, False)] and channel.published == []


def test_unexpected_failure_dead_letters_instead_of_blocking_the_queue(consumer, order, employee_user, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(
        "orders.management.commands.consume_commands.apply_transition_command",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")),
    )

    consumer._on_message(channel, _method(), _properties(), _body(_request(employee_user, order.id)))

    # a single bad command must not stall every other one behind it
    assert channel.nacked == [(1, False)] and channel.published == []


def test_expired_command_is_rejected_not_applied(consumer, order, employee_user):
    channel = FakeChannel()
    body = _body(_request(employee_user, order.id, ttl_seconds=-1))

    consumer._on_message(channel, _method(), _properties(), body)

    assert channel.acked == [1]
    row = OrderOutbox.objects.get(event_type="orders.transition.rejected.v1")
    assert row.payload["reject_code"] == "command_expired"
    assert Order.objects.get(pk=order.pk).status == "created"


def _foreign(event_type, aggregate, data):
    return json.dumps(
        {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "schema_version": 1,
            "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
            "producer": "storefront",
            "aggregate": aggregate,
            "correlation_id": str(uuid.uuid4()),
            "data": data,
        }
    ).encode()


@pytest.mark.parametrize(
    "body",
    [
        _foreign(
            "orders.order.created.v1",
            {"type": "order", "id": "7", "version": 1},
            {
                "order_id": 7,
                "customer_id": 3,
                "status": "created",
                "total": "200.00",
                "payment_method": "cash",
                "items": [
                    {
                        "source_product_id": 1,
                        "product_code": "dish_1",
                        "name": "Рамен",
                        "quantity": 2,
                        "unit_price": "100.00",
                        "line_total": "200.00",
                    }
                ],
            },
        ),
        _foreign(
            "identity.customer_changed.v1",
            {"type": "customer", "id": "3", "version": 1},
            {"customer_id": 3},
        ),
    ],
    ids=["order created", "customer changed"],
)
def test_a_valid_event_of_another_type_is_dead_lettered_and_the_consumer_keeps_running(body):
    channel = FakeChannel()

    ConsumerCommand()._on_message(channel, _method(), _properties(), body)

    assert channel.nacked == [(1, False)]
    assert channel.acked == []
    assert not CommandInbox.objects.exists()

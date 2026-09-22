from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from orders.management.commands.publish_outbox import Command as Relay
from orders.models import OrderOutbox

pytestmark = pytest.mark.django_db


def _row(aggregate_type, aggregate_id, version, event_type="order.status_changed", **kw):
    return OrderOutbox.objects.create(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=version,
        event_type=event_type,
        routing_key=event_type,
        payload={"order_id": 5},
        **kw,
    )


def _drain() -> list[int]:
    relay = Relay()
    relay._publisher.publish = Mock()
    relay._drain("w1")
    return [
        call.kwargs.get("headers", call.args[3])["aggregate_version"]
        for call in relay._publisher.publish.call_args_list
    ]


def _status(row) -> str:
    return OrderOutbox.objects.get(pk=row.pk).status


def test_a_later_event_waits_while_an_earlier_one_of_its_aggregate_backs_off():
    created = _row(
        "order", "5", 1, "order.created", next_attempt_at=timezone.now() + timedelta(minutes=2)
    )
    confirmed = _row("order", "5", 2)

    assert _drain() == []
    assert _status(confirmed) == OrderOutbox.Status.PENDING

    OrderOutbox.objects.filter(pk=created.pk).update(next_attempt_at=None)
    assert _drain() == [1]
    assert _drain() == [2]
    assert _status(confirmed) == OrderOutbox.Status.PUBLISHED


def test_one_claim_never_carries_two_events_of_one_aggregate():
    _row("order", "6", 1, "order.created")
    _row("order", "6", 2)
    _row("order", "6", 3)

    assert _drain() == [1]
    assert _drain() == [2]
    assert _drain() == [3]


def test_the_same_id_in_another_aggregate_type_is_not_held_back():
    _row("order", "7", 1, "order.created", next_attempt_at=timezone.now() + timedelta(minutes=2))
    customer = _row("customer", "7", 1, "identity.customer_changed")

    assert _drain() == [1]
    assert _status(customer) == OrderOutbox.Status.PUBLISHED


def test_a_row_the_operator_failed_no_longer_holds_its_aggregate():
    _row("order", "8", 1, "order.created", status=OrderOutbox.Status.FAILED)
    later = _row("order", "8", 2)

    assert _drain() == [2]
    assert _status(later) == OrderOutbox.Status.PUBLISHED


def test_every_writer_names_its_aggregate_type():
    with pytest.raises(IntegrityError), transaction.atomic():
        OrderOutbox.objects.create(
            aggregate_id="9", event_type="order.created", routing_key="order.created", payload={}
        )


@pytest.mark.django_db(transaction=True)
def test_the_migration_names_the_aggregate_of_rows_written_before_it():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = (
        ("orders", "0010_outbox_failed_status"),
        ("orders", "0011_outbox_aggregate_type"),
    )
    expected = {
        "order.created": "order",
        "order.status_changed": "order",
        "orders.transition.succeeded.v1": "order",
        "orders.transition.rejected.v1": "order",
        "inventory.stock_changed": "product",
        "identity.customer_changed": "customer",
        "identity.authz_changed": "authz",
        "snapshot.control": "snapshot",
    }
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([before])
        old = executor.loader.project_state([before]).apps.get_model("orders", "OrderOutbox")
        for event_type in expected:
            old.objects.create(
                aggregate_id="1", event_type=event_type, routing_key=event_type, payload={}
            )

        executor = MigrationExecutor(connection)
        executor.migrate([after])

        assert dict(OrderOutbox.objects.values_list("event_type", "aggregate_type")) == expected
    finally:
        MigrationExecutor(connection).migrate(leaves)

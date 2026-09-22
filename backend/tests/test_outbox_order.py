from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
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


def test_a_discarded_row_keeps_holding_its_aggregate():
    _row("order", "8", 1, "order.created", status=OrderOutbox.Status.FAILED)
    later = _row("order", "8", 2)

    assert _drain() == []
    assert _status(later) == OrderOutbox.Status.PENDING


def test_a_retried_discarded_row_goes_first_and_releases_the_rest():
    failed = _row("order", "9", 1, "order.created", status=OrderOutbox.Status.FAILED)
    later = _row("order", "9", 2)

    call_command("outbox", "--retry", str(failed.pk))

    assert _drain() == [1]
    assert _drain() == [2]
    assert _status(later) == OrderOutbox.Status.PUBLISHED


def _placed_and_confirmed_order(user, make_product, seed_cart):
    from orders import service as order_service

    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1})
    order = order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-resync")
    order_service.transition(order, "confirmed", expected_status="created")
    return order


def _order_rows(order):
    return OrderOutbox.objects.filter(aggregate_type="order", aggregate_id=str(order.id))


def test_resync_replaces_the_unsent_history_of_an_aggregate_with_its_current_state(
    user, make_product, seed_cart
):
    order = _placed_and_confirmed_order(user, make_product, seed_cart)
    created = _order_rows(order).get(event_type="order.created")
    call_command("outbox", "--discard", str(created.pk), "--yes", "--reason", "rejected payload")
    assert _drain_order_events(order) == []

    call_command("outbox", "--resync", f"order:{order.id}", "--yes", "--reason", "rejected payload")

    assert _drain_order_events(order) == [("order.created", 2, "confirmed")]
    assert set(_order_rows(order).exclude(status="published").values_list("status", flat=True)) == {
        OrderOutbox.Status.SUPERSEDED
    }


def _drain_order_events(order):
    relay = Relay()
    relay._publisher.publish = Mock()
    relay._drain("w1")
    return [
        (call.args[1], call.args[3]["aggregate_version"], call.args[2].get("status"))
        for call in relay._publisher.publish.call_args_list
        if call.args[3].get("aggregate_version")
        and str(call.args[2].get("order_id")) == str(order.id)
    ]


def test_resync_refuses_an_aggregate_that_has_nothing_unsent(user, make_product, seed_cart):
    order = _placed_and_confirmed_order(user, make_product, seed_cart)
    _order_rows(order).update(status=OrderOutbox.Status.PUBLISHED)

    with pytest.raises(CommandError, match="no unsent events"):
        call_command("outbox", "--resync", f"order:{order.id}", "--yes", "--reason", "nothing")

    assert not _order_rows(order).filter(status=OrderOutbox.Status.PENDING).exists()


def test_resync_refuses_when_a_consumer_already_holds_the_current_version(
    user, make_product, seed_cart
):
    order = _placed_and_confirmed_order(user, make_product, seed_cart)
    _order_rows(order).filter(event_type="order.created").update(status=OrderOutbox.Status.FAILED)
    _order_rows(order).filter(event_type="order.status_changed").update(
        status=OrderOutbox.Status.PUBLISHED
    )

    with pytest.raises(CommandError, match="already published"):
        call_command("outbox", "--resync", f"order:{order.id}", "--yes", "--reason", "late")

    assert _order_rows(order).filter(status=OrderOutbox.Status.FAILED).count() == 1


def test_resync_of_a_snapshot_run_abandons_it_without_emitting():
    started = _row("snapshot", "run-1", 1, "snapshot.control", status=OrderOutbox.Status.FAILED)
    completed = _row("snapshot", "run-1", 1, "snapshot.control")

    call_command("outbox", "--resync", "snapshot:run-1", "--yes", "--reason", "abandoned")

    assert {_status(started), _status(completed)} == {OrderOutbox.Status.SUPERSEDED}
    assert not OrderOutbox.objects.filter(status=OrderOutbox.Status.PENDING).exists()


def test_the_listing_names_a_discarded_row_that_holds_later_events(capsys):
    failed = _row("order", "11", 1, "order.created", status=OrderOutbox.Status.FAILED)
    _row("order", "11", 2)

    call_command("outbox", "--list")

    out = capsys.readouterr().out
    assert f"#{failed.pk}" in out and "holds 1 later event" in out


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

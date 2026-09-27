import datetime as dt
import uuid

import pytest
from django.db import DatabaseError
from event_contracts import (
    EVENT_ORDER_TRANSITION_REJECTED,
    EVENT_ORDER_TRANSITION_SUCCEEDED,
    parse_event,
)
from limiter_reset import command_limiter  # noqa: F401

from operations import dispatch
from operations.commands import OutcomeMismatch, create_transition_command
from operations.models import InboxEvent, InventoryProjection, OperationCommand

pytestmark = pytest.mark.django_db


def _command(order_id=1, key="k1"):
    command, _ = create_transition_command(
        actor_id=42, actor_authz_version=3, order_id=order_id, expected_status="created",
        target_status="confirmed", idempotency_key=key,
    )
    return command


def _outcome_env(command, *, succeeded=True, correlation_id=None, event_id=None):
    order_id = int(command.target)
    if succeeded:
        event_type = EVENT_ORDER_TRANSITION_SUCCEEDED
        data = {"command_id": str(command.command_id), "order_id": order_id,
                "from_status": "created", "status": "confirmed"}
    else:
        event_type = EVENT_ORDER_TRANSITION_REJECTED
        data = {"command_id": str(command.command_id), "order_id": order_id,
                "reject_code": "stale_status", "current_status": "preparing", "detail": "x"}
    return {
        "event_id": str(event_id or uuid.uuid4()),
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        "producer": "storefront",
        "aggregate": {"type": "order", "id": str(order_id), "version": 2},
        "correlation_id": str(correlation_id or command.correlation_id),
        "causation_id": str(command.request_event_id),
        "data": data,
    }


def _stock_env():
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "inventory.stock_changed.v1",
        "schema_version": 1,
        "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        "producer": "storefront",
        "aggregate": {"type": "product", "id": "dish_1", "version": 3},
        "correlation_id": str(uuid.uuid4()),
        "data": {"product_code": "dish_1", "name": "рамен", "stock_quantity": 7},
    }


def test_outcome_finalizes_command_and_records_inbox_once():
    command = _command()
    envelope, data = parse_event(_outcome_env(command))

    assert dispatch.handle(envelope, data) is True

    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.SUCCEEDED
    assert InboxEvent.objects.count() == 1


def test_outcome_failure_rolls_back_inbox_and_redelivery_completes(monkeypatch):
    command = _command()
    payload = _outcome_env(command)
    envelope, data = parse_event(payload)

    calls = {"n": 0}
    real = dispatch.apply_transition_outcome

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DatabaseError("transient failure while applying the outcome")
        return real(*args, **kwargs)

    monkeypatch.setattr(dispatch, "apply_transition_outcome", flaky)

    with pytest.raises(DatabaseError):
        dispatch.handle(envelope, data)
    assert InboxEvent.objects.count() == 0
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.PENDING

    envelope, data = parse_event(payload)
    assert dispatch.handle(envelope, data) is True
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.SUCCEEDED
    assert InboxEvent.objects.count() == 1


def test_duplicate_outcome_delivery_is_ignored():
    command = _command()
    payload = _outcome_env(command)
    envelope, data = parse_event(payload)
    dispatch.handle(envelope, data)

    envelope, data = parse_event(payload)
    assert dispatch.handle(envelope, data) is False
    assert InboxEvent.objects.count() == 1
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.SUCCEEDED


def test_outcome_for_unknown_command_rolls_back_inbox():
    command = _command()
    envelope, data = parse_event(_outcome_env(command, correlation_id=uuid.uuid4()))

    with pytest.raises(OutcomeMismatch):
        dispatch.handle(envelope, data)
    assert InboxEvent.objects.count() == 0


def test_projection_event_still_flows_through_the_dispatcher():
    envelope, data = parse_event(_stock_env())

    assert dispatch.handle(envelope, data) is True

    assert InventoryProjection.objects.get(product_code="dish_1").stock_quantity == 7
    assert InboxEvent.objects.count() == 1


def test_outcome_with_foreign_causation_rolls_back_inbox():
    command = _command()
    payload = _outcome_env(command)
    payload["causation_id"] = str(uuid.uuid4())
    envelope, data = parse_event(payload)

    with pytest.raises(OutcomeMismatch):
        dispatch.handle(envelope, data)

    assert InboxEvent.objects.count() == 0
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.PENDING


def test_outcome_without_causation_rolls_back_inbox():
    command = _command()
    payload = _outcome_env(command)
    payload.pop("causation_id")
    envelope, data = parse_event(payload)

    with pytest.raises(OutcomeMismatch):
        dispatch.handle(envelope, data)
    assert InboxEvent.objects.count() == 0


def test_redelivery_with_correct_causation_completes_after_a_rejected_one():
    command = _command()
    bad = _outcome_env(command)
    bad["causation_id"] = str(uuid.uuid4())
    envelope, data = parse_event(bad)
    with pytest.raises(OutcomeMismatch):
        dispatch.handle(envelope, data)

    envelope, data = parse_event(_outcome_env(command))
    assert dispatch.handle(envelope, data) is True
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.SUCCEEDED
    assert InboxEvent.objects.count() == 1

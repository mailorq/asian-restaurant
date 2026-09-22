import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from event_contracts import EVENT_ORDER_CREATED

from operations.management.commands.bridge_storefront_events import (
    BRIDGE_MAX_RETRIES,
    BRIDGE_RETRY_EXCHANGE,
    ORIGIN_PRODUCER,
    RELAYED_BY,
    RETRY_HEADER,
    Command,
    _map_data,
    build_envelope,
)

OCCURRED_AT = "2026-08-01T10:30:00+00:00"


def _legacy(headers_extra=None, drop_product_code=False):
    item = {
        "product_id": 3,
        "product_code": "dish_3",
        "name": "Рамен",
        "quantity": 3,
        "unit_price": "19.99",
        "line_total": "59.97",
    }
    if drop_product_code:
        del item["product_code"]
    body = json.dumps(
        {
            "order_id": 42,
            "user_id": 7,
            "status": "created",
            "total": "59.97",
            "recipient_name": "Иван",
            "payment_method": "card",
            "items": [item],
        }
    ).encode()
    headers = {
        "event_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "aggregate_version": 1,
        "occurred_at": OCCURRED_AT,
    }
    if headers_extra:
        headers.update(headers_extra)
    props = SimpleNamespace(
        type="order.created",
        message_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        content_type="application/json",
        headers=headers,
    )
    method = SimpleNamespace(delivery_tag=1, routing_key="order.created")
    return props, method, body


def _legacy_stock(snapshot=False):
    body = json.dumps({"product_code": "dish_3", "name": "Рамен", "stock_quantity": 12}).encode()
    headers = {"event_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
               "aggregate_version": 4, "occurred_at": OCCURRED_AT, "snapshot": snapshot}
    if snapshot:
        headers["snapshot_run_id"] = "run1"
    props = SimpleNamespace(type="inventory.stock_changed", message_id=str(uuid.uuid4()),
                            correlation_id=str(uuid.uuid4()), content_type="application/json", headers=headers)
    method = SimpleNamespace(delivery_tag=1, routing_key="inventory.stock_changed")
    return props, method, body


def _legacy_control(phase="completed", run_id="run1", counts=None):
    if phase == "completed":
        counts = {"product": 0, "customer": 0, "order": 0, **(counts or {})}
    else:
        counts = counts or {}
    body = json.dumps({"run_id": run_id, "phase": phase, "as_of": OCCURRED_AT, "counts": counts}).encode()
    headers = {"event_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
               "aggregate_version": 1, "occurred_at": OCCURRED_AT, "snapshot_run_id": run_id}
    props = SimpleNamespace(type="snapshot.control", message_id=str(uuid.uuid4()),
                            correlation_id=str(uuid.uuid4()), content_type="application/json", headers=headers)
    method = SimpleNamespace(delivery_tag=1, routing_key="snapshot.control")
    return props, method, body


def _cmd(publish_side_effect=None):
    cmd = Command()
    cmd.publish_channel = MagicMock()
    if publish_side_effect is not None:
        cmd.publish_channel.basic_publish.side_effect = publish_side_effect
    return cmd


def test_payment_method_is_passed_through():
    legacy = {"order_id": 1, "user_id": 2, "status": "created", "total": "59.97", "payment_method": "card",
              "items": [{"product_id": 3, "product_code": "d3", "name": "x", "quantity": 3,
                         "unit_price": "19.99", "line_total": "59.97"}]}
    assert _map_data(EVENT_ORDER_CREATED, legacy)["payment_method"] == "card"


def test_snapshot_control_maps_to_snapshot_aggregate():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy_control(phase="completed", run_id="run1", counts={"product": 2})

    cmd._on_message(ch, method, props, body)

    _, kwargs = cmd.publish_channel.basic_publish.call_args
    env = json.loads(kwargs["body"])
    assert env["aggregate"] == {"type": "snapshot", "id": "run1", "version": 1}
    assert env["snapshot_run_id"] == "run1"
    assert env["data"]["phase"] == "completed"
    assert env["data"]["counts"] == {"product": 2, "customer": 0, "order": 0}
    ch.basic_ack.assert_called_once_with(method.delivery_tag)


def test_snapshot_run_id_is_propagated():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy_stock(snapshot=True)
    props.headers["snapshot_run_id"] = "runX"

    cmd._on_message(ch, method, props, body)

    _, kwargs = cmd.publish_channel.basic_publish.call_args
    assert json.loads(kwargs["body"])["snapshot_run_id"] == "runX"


def test_stock_event_maps_to_product_aggregate():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy_stock()

    cmd._on_message(ch, method, props, body)

    _, kwargs = cmd.publish_channel.basic_publish.call_args
    env = json.loads(kwargs["body"])
    assert env["aggregate"] == {"type": "product", "id": "dish_3", "version": 4}
    assert env["data"]["stock_quantity"] == 12
    assert env["snapshot"] is False
    ch.basic_ack.assert_called_once_with(method.delivery_tag)


def test_snapshot_flag_is_propagated():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy_stock(snapshot=True)

    cmd._on_message(ch, method, props, body)

    _, kwargs = cmd.publish_channel.basic_publish.call_args
    assert json.loads(kwargs["body"])["snapshot"] is True


def test_valid_created_is_published_and_acked():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy()

    cmd._on_message(ch, method, props, body)

    cmd.publish_channel.basic_publish.assert_called_once()
    _, kwargs = cmd.publish_channel.basic_publish.call_args
    published = json.loads(kwargs["body"])
    assert published["data"]["payment_method"] == "card"
    item = published["data"]["items"][0]
    assert item["product_code"] == "dish_3"
    assert item["source_product_id"] == 3
    assert item["line_total"] == "59.97"
    ch.basic_ack.assert_called_once_with(method.delivery_tag)
    ch.basic_nack.assert_not_called()


def test_unknown_legacy_type_goes_to_dlq():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy()
    props.type = "order.deleted"

    cmd._on_message(ch, method, props, body)

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    cmd.publish_channel.basic_publish.assert_not_called()


def test_invalid_json_goes_to_dlq():
    cmd = _cmd()
    ch = MagicMock()
    props, method, _ = _legacy()

    cmd._on_message(ch, method, props, b"{not json")

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    cmd.publish_channel.basic_publish.assert_not_called()


def test_contract_violation_is_poison_not_retried():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy(drop_product_code=True)

    cmd._on_message(ch, method, props, body)

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    cmd.publish_channel.basic_publish.assert_not_called()


def test_missing_occurred_at_goes_to_dlq():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy()
    del props.headers["occurred_at"]

    cmd._on_message(ch, method, props, body)

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    cmd.publish_channel.basic_publish.assert_not_called()


def test_publish_failure_is_retried():
    cmd = _cmd(publish_side_effect=Exception("broker down"))
    ch = MagicMock()
    props, method, body = _legacy()

    cmd._on_message(ch, method, props, body)

    ch.basic_publish.assert_called_once()
    _, kwargs = ch.basic_publish.call_args
    assert kwargs["exchange"] == BRIDGE_RETRY_EXCHANGE
    assert kwargs["properties"].headers[RETRY_HEADER] == 1
    assert kwargs["properties"].type == "order.created"
    ch.basic_ack.assert_called_once_with(method.delivery_tag)
    ch.basic_nack.assert_not_called()


def test_failed_publish_discards_the_connection():
    cmd = _cmd(publish_side_effect=Exception("broker down"))
    ch = MagicMock()
    props, method, body = _legacy()

    cmd._on_message(ch, method, props, body)

    # a dead publish connection is never serviced by the consume loop, so reusing it would dead-letter every later event until someone restarts the container
    assert cmd.publish_channel is None and cmd._publish_conn is None


def test_exhausted_retries_go_to_dlq():
    cmd = _cmd(publish_side_effect=Exception("still down"))
    ch = MagicMock()
    props, method, body = _legacy({RETRY_HEADER: BRIDGE_MAX_RETRIES})

    cmd._on_message(ch, method, props, body)

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    ch.basic_publish.assert_not_called()


def test_map_data_uses_real_code_and_decimal():
    legacy = {
        "order_id": 1,
        "user_id": 2,
        "status": "created",
        "total": "59.97",
        "items": [
            {"product_id": 3, "product_code": "dish_3", "name": "x", "quantity": 3, "unit_price": "19.99", "line_total": "59.97"}
        ],
    }
    item = _map_data(EVENT_ORDER_CREATED, legacy)["items"][0]
    assert item["product_code"] == "dish_3"
    assert item["product_code"] != str(item["source_product_id"])
    assert item["source_product_id"] == 3
    assert item["line_total"] == "59.97"


def test_map_data_line_total_fallback_uses_decimal():
    legacy = {
        "order_id": 1,
        "items": [{"product_id": 3, "product_code": "dish_3", "quantity": 3, "unit_price": "19.99"}],
    }
    item = _map_data(EVENT_ORDER_CREATED, legacy)["items"][0]
    assert item["line_total"] == "59.97"


def test_envelope_preserves_origin_time_and_provenance():
    props, _method, body = _legacy()
    legacy = json.loads(body)
    envelope = build_envelope(props, EVENT_ORDER_CREATED, legacy)
    assert envelope["occurred_at"] == OCCURRED_AT
    assert envelope["producer"] == ORIGIN_PRODUCER == "storefront"
    assert envelope["relayed_by"] == RELAYED_BY

    published = _publish_and_get_envelope()
    assert published["occurred_at"] == OCCURRED_AT
    assert published["producer"] == "storefront"


def _publish_and_get_envelope():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy()
    cmd._on_message(ch, method, props, body)
    _, kwargs = cmd.publish_channel.basic_publish.call_args
    return json.loads(kwargs["body"])


def _outcome_props(event_type, causation_id):
    return SimpleNamespace(
        headers={
            "event_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "causation_id": causation_id,
            "aggregate_version": 4,
            "occurred_at": OCCURRED_AT,
            "snapshot": False,
            "snapshot_run_id": None,
        },
        message_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        type=event_type,
    )


def test_bridge_maps_transition_succeeded_and_keeps_causation():
    command_id = str(uuid.uuid4())
    causation = str(uuid.uuid4())
    legacy = {"command_id": command_id, "order_id": 42, "from_status": "created", "status": "confirmed"}

    envelope = build_envelope(
        _outcome_props("orders.transition.succeeded.v1", causation),
        "orders.transition.succeeded.v1", legacy,
    )

    assert envelope["event_type"] == "orders.transition.succeeded.v1"
    assert envelope["producer"] == ORIGIN_PRODUCER
    assert envelope["aggregate"] == {"type": "order", "id": "42", "version": 4}
    assert envelope["causation_id"] == causation
    assert envelope["data"] == legacy


def test_bridge_maps_transition_rejected():
    legacy = {"command_id": str(uuid.uuid4()), "order_id": 42,
              "reject_code": "stale_status", "current_status": "preparing", "detail": "x"}

    envelope = build_envelope(
        _outcome_props("orders.transition.rejected.v1", str(uuid.uuid4())),
        "orders.transition.rejected.v1", legacy,
    )

    assert envelope["event_type"] == "orders.transition.rejected.v1"
    assert envelope["data"]["reject_code"] == "stale_status"


def test_bridge_forwards_absent_causation_as_none():
    envelope = build_envelope(
        _outcome_props("orders.transition.succeeded.v1", None),
        "orders.transition.succeeded.v1",
        {"command_id": str(uuid.uuid4()), "order_id": 42, "from_status": "created", "status": "confirmed"},
    )
    assert envelope["causation_id"] is None


def _envelope_body(event_id=None, correlation_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "orders.transition.succeeded.v1",
        "schema_version": 1,
        "occurred_at": OCCURRED_AT,
        "producer": "storefront",
        "aggregate": {"type": "order", "id": "42", "version": 2},
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "causation_id": str(uuid.uuid4()),
        "data": {"command_id": str(uuid.uuid4()), "order_id": 42,
                 "from_status": "created", "status": "confirmed"},
    }


def _envelope_delivery(envelope):
    props = SimpleNamespace(
        type=envelope["event_type"],
        message_id=envelope["event_id"],
        correlation_id=envelope["correlation_id"],
        content_type="application/json",
        headers={"event_id": envelope["event_id"], "occurred_at": OCCURRED_AT},
    )
    method = SimpleNamespace(delivery_tag=1, routing_key="order.transition_succeeded")
    return props, method, json.dumps(envelope).encode()


def test_a_body_that_is_already_an_envelope_is_relayed_unchanged():
    cmd = _cmd()
    ch = MagicMock()
    envelope = _envelope_body()
    props, method, body = _envelope_delivery(envelope)

    cmd._on_message(ch, method, props, body)

    cmd.publish_channel.basic_publish.assert_called_once()
    _, kwargs = cmd.publish_channel.basic_publish.call_args
    assert json.loads(kwargs["body"]) == envelope
    ch.basic_ack.assert_called_once_with(method.delivery_tag)
    ch.basic_nack.assert_not_called()


def test_an_envelope_body_keeps_its_own_provenance():
    cmd = _cmd()
    envelope = _envelope_body()
    props, method, body = _envelope_delivery(envelope)

    cmd._on_message(MagicMock(), method, props, body)

    published = json.loads(cmd.publish_channel.basic_publish.call_args[1]["body"])
    assert published["event_id"] == envelope["event_id"]
    assert published["causation_id"] == envelope["causation_id"]
    assert published["occurred_at"] == envelope["occurred_at"]


def test_an_envelope_whose_type_contradicts_the_delivery_is_poison():
    cmd = _cmd()
    ch = MagicMock()
    envelope = _envelope_body()
    props, method, _ = _envelope_delivery(envelope)
    envelope["event_type"] = "orders.transition.rejected.v1"

    cmd._on_message(ch, method, props, json.dumps(envelope).encode())

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    cmd.publish_channel.basic_publish.assert_not_called()


def test_a_legacy_body_without_origin_time_is_still_refused():
    cmd = _cmd()
    ch = MagicMock()
    props, method, body = _legacy()
    props.headers.pop("occurred_at")

    cmd._on_message(ch, method, props, body)

    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)
    cmd.publish_channel.basic_publish.assert_not_called()


def test_a_retry_the_broker_cannot_route_is_dead_lettered_not_acknowledged():
    from pika.exceptions import UnroutableError

    cmd = _cmd(publish_side_effect=Exception("broker down"))
    ch = MagicMock()
    ch.basic_publish.side_effect = UnroutableError([])
    props, method, body = _legacy()

    cmd._on_message(ch, method, props, body)

    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=False)


def test_a_retry_the_broker_refused_to_store_puts_the_original_back():
    from pika.exceptions import NackError

    cmd = _cmd(publish_side_effect=Exception("broker down"))
    ch = MagicMock()
    ch.basic_publish.side_effect = NackError([])
    props, method, body = _legacy()

    cmd._on_message(ch, method, props, body)

    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(method.delivery_tag, requeue=True)


def test_the_consume_channel_confirms_publishes_before_it_consumes(monkeypatch):
    import pika as pika_module

    connection = MagicMock()
    channel = connection.channel.return_value
    channel.start_consuming.side_effect = KeyboardInterrupt
    monkeypatch.setattr(pika_module, "BlockingConnection", lambda params: connection)
    cmd = Command()
    monkeypatch.setattr(cmd, "_open_publish", lambda: None)
    monkeypatch.setattr(cmd, "_drop_publish", lambda: None)

    cmd.handle()

    calls = [c[0] for c in channel.method_calls]
    assert "confirm_delivery" in calls
    assert calls.index("confirm_delivery") < calls.index("basic_consume")

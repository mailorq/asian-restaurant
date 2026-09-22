import copy
import json
import logging
import logging.config
from io import StringIO
from unittest.mock import Mock

import pytest
from django.conf import settings

from orders.models import OrderOutbox

LOGGER = "orders.probe"


@pytest.fixture
def emitted():
    """applies the service logging configuration with the console stream captured"""
    config = copy.deepcopy(settings.LOGGING)
    stream = StringIO()
    config["handlers"]["console"]["stream"] = stream
    logging.config.dictConfig(config)
    try:
        yield stream
    finally:
        logging.config.dictConfig(settings.LOGGING)


def _records(stream) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_record_is_one_json_object_per_line(emitted):
    logging.getLogger(LOGGER).warning("outbox publish failed")

    records = _records(emitted)
    assert len(records) == 1
    assert records[0]["level"] == "WARNING"
    assert records[0]["logger"] == LOGGER
    assert records[0]["message"] == "outbox publish failed"
    assert records[0]["ts"]


def test_extra_fields_survive_formatting(emitted):
    logging.getLogger(LOGGER).warning(
        "command application failed",
        extra={"command_id": "cmd-1", "order_id": 7, "correlation_id": "c-1"},
    )

    record = _records(emitted)[0]
    assert record["command_id"] == "cmd-1"
    assert record["order_id"] == 7
    assert record["correlation_id"] == "c-1"


def test_an_exception_is_named_but_never_quoted(emitted):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger(LOGGER).exception("command poison message -> DLQ")

    record = _records(emitted)[0]
    assert record["exception_type"] == "ValueError"
    assert "boom" not in json.dumps(record), "an exception message quotes what it was raised over"
    assert record["level"] == "ERROR"


def test_unserialisable_extra_never_breaks_logging(emitted):
    import datetime as dt
    import uuid

    logging.getLogger(LOGGER).warning(
        "outbox row rejected", extra={"event_id": uuid.uuid4(), "created_at": dt.datetime.now(dt.UTC)}
    )

    record = _records(emitted)[0]
    assert isinstance(record["event_id"], str) and isinstance(record["created_at"], str)


def test_pika_chatter_is_kept_out_of_the_stream(emitted):
    logging.getLogger("pika.adapters.blocking_connection").info("Created channel=1")

    assert _records(emitted) == []


def _nested_log():
    """stands in for a log emitted deeper in the stack, far from the message boundary"""
    logging.getLogger("orders.deep").warning("transition refused")


def test_bound_context_reaches_a_record_emitted_deeper_in_the_stack(emitted):
    from config.jsonlog import log_context

    with log_context(correlation_id="c-1", command_id="cmd-1"):
        _nested_log()

    record = _records(emitted)[0]
    assert record["correlation_id"] == "c-1"
    assert record["command_id"] == "cmd-1"


def test_context_does_not_leak_past_its_block(emitted):
    from config.jsonlog import log_context

    with log_context(correlation_id="c-1"):
        pass
    _nested_log()

    assert "correlation_id" not in _records(emitted)[0]


def test_an_explicit_field_wins_over_the_bound_context(emitted):
    from config.jsonlog import log_context

    with log_context(correlation_id="from-context"):
        logging.getLogger(LOGGER).warning("x", extra={"correlation_id": "from-call"})

    assert _records(emitted)[0]["correlation_id"] == "from-call"


def test_absent_identifiers_are_not_written_as_null(emitted):
    from config.jsonlog import log_context

    with log_context(correlation_id="c-1", causation_id=None):
        _nested_log()

    assert "causation_id" not in _records(emitted)[0]


@pytest.mark.django_db
def test_a_failed_publish_is_logged_with_the_row_identifiers(emitted, monkeypatch):
    from orders.management.commands.publish_outbox import Command as Relay

    row = OrderOutbox.objects.create(
        aggregate_type="order",
        aggregate_id="1", event_type="order.created", routing_key="order.created",
        payload={"order_id": 1},
    )
    relay = Relay()
    [claimed] = relay._claim("w1")
    monkeypatch.setattr(relay._publisher, "publish", Mock(side_effect=Exception("broker down")))

    relay._publish_one("w1", claimed)

    record = next(r for r in _records(emitted) if r["level"] == "ERROR")
    assert record["outbox_id"] == row.pk
    assert record["event_type"] == "order.created"
    assert record["attempts"] == 1
    assert record["event_id"] == str(row.event_id)
    assert record["correlation_id"] == str(row.correlation_id)
    assert record["exception_type"] == "Exception"


@pytest.mark.django_db
def test_the_command_consumer_binds_the_envelope_for_records_made_deeper(emitted):
    import datetime as dt
    import types
    import uuid as _uuid
    from unittest.mock import MagicMock

    from orders.management.commands.consume_commands import Command as Consumer

    correlation = str(_uuid.uuid4())
    command_id = str(_uuid.uuid4())
    # an outcome event on the command queue: the consumer refuses it, and that call site
    # passes no identifiers of its own, so anything in the record came from the binding
    envelope = {
        "event_id": str(_uuid.uuid4()), "event_type": "orders.transition.succeeded.v1",
        "schema_version": 1, "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        "producer": "storefront", "aggregate": {"type": "order", "id": "42", "version": 2},
        "correlation_id": correlation, "causation_id": str(_uuid.uuid4()),
        "data": {"command_id": command_id, "order_id": 42,
                 "from_status": "created", "status": "confirmed"},
    }
    method = types.SimpleNamespace(delivery_tag=1, routing_key="orders.transition.succeeded")
    properties = types.SimpleNamespace(headers={}, message_id=str(_uuid.uuid4()), correlation_id=None)

    Consumer()._on_message(MagicMock(), method, properties, json.dumps(envelope).encode())

    record = next(r for r in _records(emitted) if "-> DLQ" in r["message"])
    assert record["correlation_id"] == correlation
    assert record["command_id"] == command_id
    assert record["event_type"] == "orders.transition.succeeded.v1"

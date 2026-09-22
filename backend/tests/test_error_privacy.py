"""
what an error may carry out of the storefront relay

an outbox row is an order event: name, phone, address. pika logs a returned message with a
prefix of its body, and a failed publish was storing the raw exception text
"""

import copy
import logging
import logging.config
from io import StringIO
from unittest.mock import Mock

import pytest
from django.conf import settings
from pika.exceptions import UnroutableError

from orders.models import OrderOutbox

pytestmark = pytest.mark.django_db

PHONE = "+380991234567"
ADDRESS = "Kyiv, Secret Street 1, kv 5"


@pytest.fixture
def emitted():
    config = copy.deepcopy(settings.LOGGING)
    stream = StringIO()
    config["handlers"]["console"]["stream"] = stream
    logging.config.dictConfig(config)
    try:
        yield stream
    finally:
        logging.config.dictConfig(settings.LOGGING)


def _row():
    return OrderOutbox.objects.create(
        aggregate_type="order",
        aggregate_id="1", event_type="order.created", routing_key="order.created",
        payload={"order_id": 1, "phone": PHONE, "address": ADDRESS},
    )


def test_a_failed_publish_records_the_shape_of_the_error_not_its_text(monkeypatch):
    from orders.management.commands.publish_outbox import Command as Relay

    row = _row()
    relay = Relay()
    [claimed] = relay._claim("w1")
    monkeypatch.setattr(relay._publisher, "publish",
                        Mock(side_effect=UnroutableError([f"body={ADDRESS} {PHONE}"])))

    relay._publish_one("w1", claimed)

    row.refresh_from_db()
    assert PHONE not in row.last_error and ADDRESS not in row.last_error
    assert "UnroutableError" in row.last_error


def test_the_broker_returning_a_message_does_not_log_its_body(emitted):
    logging.getLogger("pika.adapters.blocking_connection").warning(
        "Published message was returned: _delivery_confirmation=True; channel=1; "
        "body_size=120; body_prefix=b'{\"phone\": \"%s\", \"address\": \"%s\"}'", PHONE, ADDRESS
    )

    written = emitted.getvalue()
    assert written, "the warning itself is useful and must still be emitted"
    assert PHONE not in written and ADDRESS not in written
    assert "NO_ROUTE" in written or "returned" in written

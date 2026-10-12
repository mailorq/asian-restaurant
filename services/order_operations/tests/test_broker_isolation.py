"""
vhost and resource isolation of the application credentials on a provisioned broker

skipped unless BROKER_ISOLATION_HOST and the four passwords point at a provisioned broker, because these assert
broker-side authorisation rather than application behaviour. every check here is a refusal or a bare connect:
nothing is declared, published or consumed where a user is allowed to
"""

import os

import pika
import pytest
from pika.exceptions import (
    ChannelClosedByBroker,
    ConnectionClosedByBroker,
    ProbableAccessDeniedError,
)

HOST = os.environ.get("BROKER_ISOLATION_HOST")
PASSWORDS = {
    "storefront_app": os.environ.get("STOREFRONT_MQ_PASSWORD"),
    "operations_consumer": os.environ.get("OPERATIONS_MQ_PASSWORD"),
    "operations_bridge": os.environ.get("BRIDGE_MQ_PASSWORD"),
    "operations_commands": os.environ.get("OPERATIONS_COMMANDS_MQ_PASSWORD"),
}

# the release smoke sets REQUIRE_BROKER_ISOLATION_TESTS, so a missing broker fails it instead of skipping these
if os.environ.get("REQUIRE_BROKER_ISOLATION_TESTS") == "1" and not (HOST and all(PASSWORDS.values())):
    raise RuntimeError("REQUIRE_BROKER_ISOLATION_TESTS is set but the broker host or a password is missing")

pytestmark = pytest.mark.skipif(not HOST or not all(PASSWORDS.values()), reason="BROKER_ISOLATION_HOST not configured")


def _connect(user: str, vhost: str) -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=HOST, virtual_host=vhost, credentials=pika.PlainCredentials(user, PASSWORDS[user]),
        connection_attempts=1, blocked_connection_timeout=5,
    ))


def _refused(action) -> None:
    with pytest.raises(ChannelClosedByBroker) as refused:
        action()
    assert refused.value.reply_code == 403, refused.value


@pytest.mark.parametrize("user, vhost", [
    ("storefront_app", "storefront"),
    ("operations_consumer", "operations"),
    ("operations_bridge", "storefront"),
    ("operations_bridge", "operations"),
    ("operations_commands", "storefront"),
])
def test_a_user_opens_the_vhosts_it_was_given(user, vhost):
    _connect(user, vhost).close()


@pytest.mark.parametrize("user, vhost", [
    ("storefront_app", "operations"),
    ("storefront_app", "/"),
    ("operations_consumer", "storefront"),
    ("operations_consumer", "/"),
    ("operations_bridge", "/"),
    ("operations_commands", "operations"),
    ("operations_commands", "/"),
])
def test_a_user_cannot_open_a_vhost_it_was_not_given(user, vhost):
    with pytest.raises((ProbableAccessDeniedError, ConnectionClosedByBroker)) as refused:
        _connect(user, vhost).close()
    assert "vhost" in str(refused.value).lower() or getattr(refused.value, "reply_code", None) == 530


def test_the_bridge_names_only_its_own_resources_in_storefront():
    conn = _connect("operations_bridge", "storefront")
    try:
        _refused(lambda: conn.channel().queue_declare("orders.copy", auto_delete=True))
        channel = conn.channel()
        channel.confirm_delivery()
        _refused(lambda: channel.basic_publish("commands", "orders.transition.requested", b"{}"))
    finally:
        if conn.is_open:
            conn.close()


def test_the_bridge_only_publishes_in_operations():
    conn = _connect("operations_bridge", "operations")
    try:
        _refused(lambda: conn.channel().basic_get("operations.projection"))
        _refused(lambda: conn.channel().queue_declare("operations.events.copy", auto_delete=True))
    finally:
        if conn.is_open:
            conn.close()


def test_the_consumer_stays_within_operations_names():
    conn = _connect("operations_consumer", "operations")
    try:
        _refused(lambda: conn.channel().queue_declare("storefront.copy", auto_delete=True))
        _refused(lambda: conn.channel().basic_get("orders"))
    finally:
        if conn.is_open:
            conn.close()

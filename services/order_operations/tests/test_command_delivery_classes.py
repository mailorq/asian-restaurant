"""
the relay must answer three different questions about delivery, not one

a returned publish proves the message was not routed; a channel the broker closed may have been
closed after it accepted the message; a lost confirm says nothing at all. treating them alike
left two of them retrying forever with the command pending and nothing to act on
"""

import pytest
from limiter_reset import command_limiter  # noqa: F401
from pika.exceptions import AMQPConnectionError, ChannelClosedByBroker, UnroutableError

from operations import commands
from operations.commands import create_transition_command
from operations.management.commands import publish_commands
from operations.models import OperationCommand, OperationsOutbox

pytestmark = pytest.mark.django_db

WORKER = "relay-test"


class FakeChannel:
    def __init__(self) -> None:
        self.is_open = True
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _command(key="k1"):
    command, _ = create_transition_command(
        actor_id=42, actor_authz_version=3, order_id=1, expected_status="created",
        target_status="confirmed", idempotency_key=key,
    )
    return command


def _relay(monkeypatch, exc):
    relay = publish_commands.Command()
    channel = FakeChannel()

    def _open():
        relay._channel = channel
        return channel

    def publish(*args, **kwargs):
        raise exc

    monkeypatch.setattr(relay, "_open", _open)
    monkeypatch.setattr(publish_commands.messaging, "publish_envelope", publish)
    return relay


def _reload(command):
    return OperationCommand.objects.get(pk=command.pk)


def _row(command):
    return OperationsOutbox.objects.get(command=command)


def _spend(command, attempts):
    OperationsOutbox.objects.filter(command=command).update(attempts=attempts)


def test_a_returned_publish_retries_on_a_fixed_rare_interval(monkeypatch):
    command = _command()

    _relay(monkeypatch, UnroutableError([]))._drain(WORKER)

    row = _row(command)
    assert row.status == OperationsOutbox.Status.PENDING
    assert row.attempts == 1
    waited = (row.next_attempt_at - commands.timezone.now()).total_seconds()
    assert waited <= publish_commands.TOPOLOGY_RETRY_SECONDS + 1, "topology repair is an operator action, not an exponential backoff"


def test_a_command_the_broker_never_routed_is_closed_once_the_budget_is_spent(monkeypatch):
    command = _command()
    _spend(command, publish_commands.TOPOLOGY_MAX_ATTEMPTS - 1)

    _relay(monkeypatch, UnroutableError([]))._drain(WORKER)

    resolved = _reload(command)
    assert resolved.status == OperationCommand.Status.REJECTED, "every attempt came back, so non-delivery is established"
    assert resolved.result_code == "undeliverable"
    assert _row(command).status == OperationsOutbox.Status.FAILED


def test_a_returned_publish_is_never_called_delivery_unknown(monkeypatch):
    command = _command()
    _spend(command, publish_commands.TOPOLOGY_MAX_ATTEMPTS - 1)

    _relay(monkeypatch, UnroutableError([]))._drain(WORKER)

    assert _reload(command).status != OperationCommand.Status.DISPATCH_FAILED


@pytest.mark.parametrize("code,text", [
    (403, "ACCESS_REFUSED"), (404, "NOT_FOUND - no exchange"), (406, "PRECONDITION_FAILED"),
])
def test_a_broker_refusing_the_publish_is_a_verdict_not_uncertainty(monkeypatch, code, text):
    command = _command(key=f"k-{code}")
    _spend(command, publish_commands.TOPOLOGY_MAX_ATTEMPTS - 1)

    _relay(monkeypatch, ChannelClosedByBroker(code, text))._drain(WORKER)

    resolved = _reload(command)
    assert resolved.status == OperationCommand.Status.REJECTED, (
        f"{code} rejects the publish itself, so nothing was delivered"
    )
    assert resolved.result_code == "undeliverable"


def test_a_channel_closed_for_any_other_reason_stays_delivery_unknown(monkeypatch):
    command = _command()
    _spend(command, publish_commands.MAX_ATTEMPTS - 1)

    # an internal error or a resource alarm can close the channel after the message was accepted
    _relay(monkeypatch, ChannelClosedByBroker(541, "INTERNAL_ERROR"))._drain(WORKER)

    resolved = _reload(command)
    assert resolved.status == OperationCommand.Status.DISPATCH_FAILED, (
        "this close can follow acceptance, so it is not proof of anything"
    )


def test_a_lost_confirm_still_ends_in_delivery_unknown(monkeypatch):
    command = _command()
    _spend(command, publish_commands.MAX_ATTEMPTS - 1)

    _relay(monkeypatch, AMQPConnectionError("gone"))._drain(WORKER)

    assert _reload(command).status == OperationCommand.Status.DISPATCH_FAILED


def test_an_envelope_that_violates_the_contract_is_quarantined_at_once(monkeypatch):
    command = _command()
    OperationsOutbox.objects.filter(command=command).update(payload={"broken": True})

    publish_commands.Command()._drain(WORKER)

    resolved = _reload(command)
    assert resolved.status == OperationCommand.Status.REJECTED, "a deterministic failure must not be retried"
    assert resolved.result_code == "invalid_payload"
    assert _row(command).status == OperationsOutbox.Status.FAILED


def test_no_failure_class_can_retry_without_a_budget():
    caps = (publish_commands.MAX_ATTEMPTS, publish_commands.TOPOLOGY_MAX_ATTEMPTS)
    assert all(isinstance(c, int) and 0 < c < 100 for c in caps)


def test_the_worst_case_retry_schedule_fits_inside_the_command_deadline():
    """a command that outlives its own retries can only ever arrive expired"""
    network = sum(
        publish_commands._backoff(a).total_seconds()
        for a in range(1, publish_commands.MAX_ATTEMPTS)
    )
    topology = publish_commands.TOPOLOGY_RETRY_SECONDS * (publish_commands.TOPOLOGY_MAX_ATTEMPTS - 1)
    broker_timeouts = 30  # allowance for the attempts themselves blocking on a dead broker

    assert max(network, topology) + broker_timeouts < commands.COMMAND_TTL.total_seconds()


def test_a_system_verdict_is_not_recorded_as_the_employee_acting(monkeypatch):
    from operations.models import OperationAuditLog

    command = _command()
    _spend(command, publish_commands.TOPOLOGY_MAX_ATTEMPTS - 1)

    _relay(monkeypatch, UnroutableError([]))._drain(WORKER)

    entry = OperationAuditLog.objects.get(command_id=command.command_id, result="undeliverable")
    assert entry.actor_role == "system", "the relay closed this, not the employee who asked"
    assert entry.actor_id != str(command.actor_id)


def test_the_verdict_shown_to_the_employee_carries_no_broker_internals(monkeypatch):
    command = _command()
    _spend(command, publish_commands.TOPOLOGY_MAX_ATTEMPTS - 1)

    _relay(monkeypatch, UnroutableError([]))._drain(WORKER)

    detail = _reload(command).result_detail
    assert "unroutable message(s)" not in detail, "pika's wording is an internal, not an answer"
    assert detail, "the employee still needs to be told something"
    assert "returned" not in detail.lower() or "broker" in detail.lower()

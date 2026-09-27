"""
what an error may carry out of the relay

a command's reason is text an employee typed, and a rejected envelope puts it straight into a
pydantic message as input_value. that string was going into OperationsOutbox.last_error and into a logged traceback
"""

import json

import pytest
from limiter_reset import command_limiter  # noqa: F401
from pika.exceptions import ChannelClosedByBroker, UnroutableError

from operations import commands
from operations.commands import create_transition_command
from operations.management.commands import publish_commands
from operations.models import OperationCommand, OperationsOutbox

pytestmark = pytest.mark.django_db

WORKER = "relay-test"
ADDRESS = "Kyiv, Secret Street 1, kv 5"
PHONE = "+380991234567"
PII = f"{ADDRESS}, {PHONE} " * 8  # longer than the contract allows, so it is the rejected field


class FakeChannel:
    is_open = True

    def close(self):
        self.is_open = False


def _command(key="k1"):
    command, _ = create_transition_command(
        actor_id=42, actor_authz_version=3, order_id=1, expected_status="created",
        target_status="confirmed", idempotency_key=key,
    )
    return command


def _relay(monkeypatch, exc=None):
    relay = publish_commands.Command()
    channel = FakeChannel()
    monkeypatch.setattr(relay, "_open", lambda: channel)
    if exc is not None:
        def publish(*args, **kwargs):
            raise exc
        monkeypatch.setattr(publish_commands.messaging, "publish_envelope", publish)
    return relay


def _with_pii(command):
    row = OperationsOutbox.objects.get(command=command)
    OperationsOutbox.objects.filter(pk=row.pk).update(payload={**row.payload, "reason": PII})


def _leaked(text: str) -> bool:
    return ADDRESS in text or PHONE in text


def test_an_envelope_the_contract_rejects_leaves_no_typed_text_in_the_row(monkeypatch):
    command = _command()
    _with_pii(command)

    _relay(monkeypatch)._drain(WORKER)

    row = OperationsOutbox.objects.get(command=command)
    assert not _leaked(row.last_error), f"last_error carries what the employee typed: {row.last_error}"
    assert row.last_error, "the operator still needs to know why it was rejected"


def test_the_rejection_is_still_classified_usefully(monkeypatch):
    command = _command()
    _with_pii(command)

    _relay(monkeypatch)._drain(WORKER)

    row = OperationsOutbox.objects.get(command=command)
    assert "contract" in row.last_error or "Validation" in row.last_error
    assert "reason" in row.last_error, "the field that failed is safe to name, its value is not"


def test_nothing_typed_by_the_employee_reaches_the_log(monkeypatch, caplog):
    command = _command()
    _with_pii(command)

    with caplog.at_level("DEBUG"):
        _relay(monkeypatch)._drain(WORKER)

    dumped = json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert not _leaked(dumped), "the record or its traceback carries the typed text"


def test_the_answer_given_to_the_employee_stays_generic(monkeypatch):
    command = _command()
    _with_pii(command)

    _relay(monkeypatch)._drain(WORKER)

    resolved = OperationCommand.objects.get(pk=command.pk)
    assert resolved.result_code == commands.CODE_INVALID_PAYLOAD
    assert not _leaked(resolved.result_detail)


@pytest.mark.parametrize("exc", [UnroutableError([]), ChannelClosedByBroker(404, "NOT_FOUND")])
def test_a_broker_failure_is_stored_as_a_classification_not_a_sentence(monkeypatch, exc):
    command = _command(key=f"k-{type(exc).__name__}")

    _relay(monkeypatch, exc)._drain(WORKER)

    row = OperationsOutbox.objects.get(command=command)
    assert type(exc).__name__ in row.last_error
    assert "\n" not in row.last_error

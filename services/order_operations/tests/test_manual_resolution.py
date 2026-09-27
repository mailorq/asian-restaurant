import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from event_contracts import OrderTransitionSucceededData
from limiter_reset import command_limiter  # noqa: F401

from operations import commands
from operations.commands import CommandNotResolvable, create_transition_command, resolve_manually
from operations.models import OperationAuditLog, OperationCommand, OperationsOutbox

pytestmark = pytest.mark.django_db


def _command(key="k1"):
    command, _ = create_transition_command(
        actor_id=42, actor_authz_version=3, order_id=1, expected_status="created",
        target_status="confirmed", idempotency_key=key,
    )
    return command


def _succeeded(command):
    return OrderTransitionSucceededData(
        command_id=command.command_id, order_id=int(command.target),
        from_status="created", status="confirmed",
    )


def _stuck(status):
    command = _command()
    OperationCommand.objects.filter(pk=command.pk).update(status=status)
    return OperationCommand.objects.get(pk=command.pk)


def test_a_timed_out_command_can_be_closed_by_an_operator():
    command = _stuck(OperationCommand.Status.TIMED_OUT)

    resolved = resolve_manually(command, outcome="succeeded", operator="ops-7",
                                reason="order 1 is confirmed in the storefront")

    assert resolved.status == OperationCommand.Status.SUCCEEDED
    assert resolved.result_code == "resolved_manually"
    assert "ops-7" in resolved.result_detail


def test_a_dispatch_failed_command_can_be_closed_as_rejected():
    command = _stuck(OperationCommand.Status.DISPATCH_FAILED)

    resolved = resolve_manually(command, outcome="rejected", operator="ops-7", reason="never applied")

    assert resolved.status == OperationCommand.Status.REJECTED


def test_the_resolution_is_written_to_the_audit_log():
    command = _stuck(OperationCommand.Status.TIMED_OUT)

    resolve_manually(command, outcome="succeeded", operator="ops-7", reason="verified")

    entry = OperationAuditLog.objects.get(command_id=command.command_id, result="resolved_manually")
    assert entry.actor_id == "ops-7"


def test_a_live_command_is_never_resolvable():
    for status in (OperationCommand.Status.PENDING, OperationCommand.Status.DISPATCHED):
        command = _stuck(status)
        with pytest.raises(CommandNotResolvable):
            resolve_manually(command, outcome="succeeded", operator="ops-7", reason="x")
        assert OperationCommand.objects.get(pk=command.pk).status == status


def test_a_terminal_command_is_left_alone():
    command = _stuck(OperationCommand.Status.SUCCEEDED)

    with pytest.raises(CommandNotResolvable):
        resolve_manually(command, outcome="rejected", operator="ops-7", reason="x")

    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.SUCCEEDED


def test_an_anonymous_or_unexplained_resolution_is_refused():
    command = _stuck(OperationCommand.Status.TIMED_OUT)

    for operator, reason in (("", "why"), ("ops-7", "")):
        with pytest.raises(ValueError):
            resolve_manually(command, outcome="succeeded", operator=operator, reason=reason)
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.TIMED_OUT


def test_an_unknown_outcome_is_refused():
    command = _stuck(OperationCommand.Status.TIMED_OUT)

    with pytest.raises(ValueError):
        resolve_manually(command, outcome="maybe", operator="ops-7", reason="x")


def test_the_automatic_paths_gain_no_way_to_close_a_command():
    command = _stuck(OperationCommand.Status.TIMED_OUT)

    assert commands.mark_timed_out(command).status == OperationCommand.Status.TIMED_OUT
    assert commands.mark_dispatch_failed(command).status == OperationCommand.Status.TIMED_OUT
    assert commands.sweep_expired()["timed_out"] == 0


def test_a_late_outcome_overrules_a_wrong_verdict_and_records_it():
    command = _stuck(OperationCommand.Status.TIMED_OUT)
    resolve_manually(command, outcome="rejected", operator="ops-7", reason="assumed lost")

    result = commands.apply_transition_outcome(
        _succeeded(command), correlation_id=command.correlation_id,
        causation_id=command.request_event_id,
    )

    assert result.status == OperationCommand.Status.SUCCEEDED
    assert result.result_code == ""
    assert OperationAuditLog.objects.filter(
        command_id=command.command_id, result="manual_resolution_overruled"
    ).exists()


def test_a_late_outcome_agreeing_with_the_operator_leaves_the_record_alone():
    command = _stuck(OperationCommand.Status.TIMED_OUT)
    resolve_manually(command, outcome="succeeded", operator="ops-7", reason="verified")

    result = commands.apply_transition_outcome(
        _succeeded(command), correlation_id=command.correlation_id,
        causation_id=command.request_event_id,
    )

    assert result.status == OperationCommand.Status.SUCCEEDED
    assert result.result_detail == "ops-7: verified"
    assert not OperationAuditLog.objects.filter(result="manual_resolution_overruled").exists()


def test_an_outcome_of_another_command_never_overrules_a_verdict():
    command = _stuck(OperationCommand.Status.TIMED_OUT)
    resolve_manually(command, outcome="rejected", operator="ops-7", reason="assumed lost")

    with pytest.raises(commands.OutcomeMismatch):
        commands.apply_transition_outcome(
            _succeeded(command), correlation_id=command.correlation_id,
            causation_id=uuid.uuid4(),
        )

    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.REJECTED


def test_the_cli_resolves_a_stuck_command():
    command = _stuck(OperationCommand.Status.TIMED_OUT)

    call_command("resolve_command", str(command.command_id), "--outcome", "succeeded",
                 "--operator", "ops-7", "--reason", "verified in the storefront")

    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.SUCCEEDED


def test_the_cli_refuses_a_live_command_and_an_unknown_id():
    command = _stuck(OperationCommand.Status.DISPATCHED)

    with pytest.raises(CommandError):
        call_command("resolve_command", str(command.command_id), "--outcome", "succeeded",
                     "--operator", "ops-7", "--reason", "x")
    with pytest.raises(CommandError):
        call_command("resolve_command", "not-a-uuid", "--outcome", "succeeded",
                     "--operator", "ops-7", "--reason", "x")

    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.DISPATCHED


def test_the_outbox_row_is_closed_with_the_command():
    command = _stuck(OperationCommand.Status.TIMED_OUT)
    row = OperationsOutbox.objects.get(command_id=command.pk)
    assert row.status == OperationsOutbox.Status.PENDING

    resolve_manually(command, outcome="succeeded", operator="ops-7", reason="verified")

    row.refresh_from_db()
    assert row.status == OperationsOutbox.Status.SETTLED, "a resolved command must not be republished"

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from event_contracts import OrderTransitionRejectedData, OrderTransitionSucceededData
from limiter_reset import command_limiter  # noqa: F401
from pydantic import ValidationError

from operations import commands
from operations.commands import CommandConflict, OutcomeMismatch, create_transition_command
from operations.models import OperationAuditLog, OperationCommand, OperationsOutbox

pytestmark = pytest.mark.django_db


def _create(actor_id=42, order_id=1, expected="created", target="confirmed", key="k1", reason="",
            authz_version=3):
    return create_transition_command(
        actor_id=actor_id, actor_authz_version=authz_version, order_id=order_id,
        expected_status=expected, target_status=target, idempotency_key=key, reason=reason,
    )


def _dispatch(command):
    """claim and confirm; a command only reaches dispatched through a publisher confirm"""
    row = OperationsOutbox.objects.get(command=command)
    _, claimed = commands.claim_or_expire(row.pk, worker="w1", lease=timedelta(seconds=60))
    commands.confirm_dispatch(claimed)


def _unresolved(command):
    """delivery is unresolved only once the request reached the network and its time ran out"""
    row = OperationsOutbox.objects.get(command=command)
    commands.claim_or_expire(row.pk, worker="w1", lease=timedelta(seconds=60))
    OperationsOutbox.objects.filter(pk=row.pk).update(locked_until=None)
    OperationCommand.objects.filter(pk=command.pk).update(
        deadline_at=timezone.now() - timedelta(seconds=1)
    )


def _succeeded(command, from_status="created", status="confirmed"):
    return OrderTransitionSucceededData(
        command_id=command.command_id, order_id=int(command.target),
        from_status=from_status, status=status,
    )


def _rejected(command, code="stale_status", current="preparing"):
    return OrderTransitionRejectedData(
        command_id=command.command_id, order_id=int(command.target),
        reject_code=code, current_status=current, detail="x",
    )


def test_create_persists_command_outbox_and_audit():
    command, created = _create()
    assert created is True
    assert command.status == OperationCommand.Status.PENDING
    assert command.actor_id == 42 and command.target == "1" and command.deadline_at is not None
    outbox = OperationsOutbox.objects.get()
    assert outbox.event_type == "orders.transition.requested.v1"
    assert outbox.correlation_id == command.correlation_id
    assert outbox.payload["command_id"] == str(command.command_id)
    audit = OperationAuditLog.objects.get()
    assert audit.command_id == command.command_id and audit.result == "requested"


def test_create_is_idempotent_for_same_actor_key_and_request():
    first, c1 = _create()
    second, c2 = _create()
    assert c1 is True and c2 is False and first.pk == second.pk
    assert OperationCommand.objects.count() == 1
    assert OperationsOutbox.objects.count() == 1  # no duplicate command event on replay


def test_same_key_different_request_conflicts():
    _create(target="confirmed")
    with pytest.raises(CommandConflict):
        _create(target="cancelled")


def test_same_key_different_actor_is_separate():
    a, _ = _create(actor_id=1, key="shared")
    b, _ = _create(actor_id=2, key="shared")
    assert a.pk != b.pk and OperationCommand.objects.count() == 2


def test_blank_idempotency_key_rejected():
    with pytest.raises(ValueError):
        _create(key="  ")


def test_equal_expected_and_target_rejected():
    with pytest.raises(ValidationError):
        _create(expected="confirmed", target="confirmed")


def test_timed_out_then_late_success_finalizes_succeeded():
    command, _ = _create()
    _unresolved(command)
    commands.mark_timed_out(command)
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.TIMED_OUT
    result = commands.apply_transition_outcome(_succeeded(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert result.status == OperationCommand.Status.SUCCEEDED  # late outcome wins, not stuck


def test_dispatched_then_success():
    command, _ = _create()
    _dispatch(command)
    result = commands.apply_transition_outcome(_succeeded(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert result.status == OperationCommand.Status.SUCCEEDED


def test_rejected_outcome_records_code_and_detail():
    command, _ = _create()
    _dispatch(command)
    result = commands.apply_transition_outcome(_rejected(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert result.status == OperationCommand.Status.REJECTED
    assert result.result_code == "stale_status" and result.result_detail == "x"


def test_outcome_idempotent_on_terminal():
    command, _ = _create()
    commands.apply_transition_outcome(_succeeded(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    again = commands.apply_transition_outcome(_rejected(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert again.status == OperationCommand.Status.SUCCEEDED


def test_dispatch_failed_then_late_success_finalizes_succeeded():
    command, _ = _create()
    commands.mark_dispatch_failed(command)
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.DISPATCH_FAILED
    # a missing publisher confirm is unknown delivery, not proof of non-delivery: a late outcome
    # must still finalise the command rather than leave it stuck
    result = commands.apply_transition_outcome(_succeeded(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert result.status == OperationCommand.Status.SUCCEEDED


def test_dispatch_failed_then_late_reject_finalizes_rejected():
    command, _ = _create()
    commands.mark_dispatch_failed(command)
    result = commands.apply_transition_outcome(_rejected(command), correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert result.status == OperationCommand.Status.REJECTED


def test_idempotency_key_too_long_rejected():
    with pytest.raises(ValueError):
        _create(key="x" * 201)


def test_dispatch_failed_then_redispatch():
    command, _ = _create()
    commands.mark_dispatch_failed(command)
    _dispatch(command)  # successful re-publish after retry
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.DISPATCHED


def test_timeout_before_the_deadline_leaves_the_command_dispatched():
    command, _ = _create()
    _dispatch(command)

    # the deadline still holds, so delivery is not yet unresolved
    assert commands.mark_timed_out(command).status == OperationCommand.Status.DISPATCHED


def test_timed_out_is_not_redispatched():
    command, _ = _create()
    _unresolved(command)
    commands.mark_timed_out(command)

    _dispatch(command)  # a later confirm for the same request

    stored = OperationCommand.objects.get(pk=command.pk)
    assert stored.status == OperationCommand.Status.TIMED_OUT  # deadline elapsed; stays a signal
    # the confirm is still a fact, so the row is finished and never handed out again
    assert OperationsOutbox.objects.get(command=command).status == OperationsOutbox.Status.PUBLISHED


def test_succeeded_outcome_wrong_from_status_raises():
    command, _ = _create()  # created -> confirmed
    bad = OrderTransitionSucceededData(command_id=command.command_id, order_id=int(command.target),
                                       from_status="preparing", status="confirmed")
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(bad, correlation_id=command.correlation_id, causation_id=command.request_event_id)


def test_succeeded_outcome_wrong_target_does_not_finalize():
    command, _ = _create()  # created -> confirmed
    bad = OrderTransitionSucceededData(command_id=command.command_id, order_id=int(command.target),
                                       from_status="created", status="cancelled")
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(bad, correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.PENDING


def test_rejected_stale_current_equals_expected_raises():
    command, _ = _create()  # expected_status = created
    bad = OrderTransitionRejectedData(command_id=command.command_id, order_id=int(command.target),
                                      reject_code="stale_status", current_status="created")
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(bad, correlation_id=command.correlation_id, causation_id=command.request_event_id)


def test_rejected_invalid_transition_current_must_equal_expected():
    command, _ = _create()  # expected_status = created
    bad = OrderTransitionRejectedData(command_id=command.command_id, order_id=int(command.target),
                                      reject_code="invalid_transition", current_status="preparing")
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(bad, correlation_id=command.correlation_id, causation_id=command.request_event_id)


def test_rejected_invalid_transition_matching_expected_ok():
    command, _ = _create()  # expected_status = created
    ok = OrderTransitionRejectedData(command_id=command.command_id, order_id=int(command.target),
                                     reject_code="invalid_transition", current_status="created")
    result = commands.apply_transition_outcome(ok, correlation_id=command.correlation_id, causation_id=command.request_event_id)
    assert result.status == OperationCommand.Status.REJECTED


def test_outcome_correlation_mismatch_raises():
    command, _ = _create()
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(_succeeded(command), correlation_id=uuid.uuid4(), causation_id=command.request_event_id)


def test_outcome_unknown_command_raises():
    command, _ = _create()
    ghost = OrderTransitionSucceededData(command_id=uuid.uuid4(), order_id=1,
                                         from_status="created", status="confirmed")
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(ghost, correlation_id=command.correlation_id, causation_id=command.request_event_id)


def test_outcome_order_id_mismatch_raises():
    command, _ = _create(order_id=1)
    bad = OrderTransitionSucceededData(command_id=command.command_id, order_id=999,
                                       from_status="created", status="confirmed")
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(bad, correlation_id=command.correlation_id, causation_id=command.request_event_id)


def test_outcome_with_foreign_causation_is_rejected():
    command, _ = _create()
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(
            _succeeded(command), correlation_id=command.correlation_id, causation_id=uuid.uuid4()
        )
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.PENDING


def test_outcome_without_causation_is_rejected():
    command, _ = _create()
    with pytest.raises(OutcomeMismatch):
        commands.apply_transition_outcome(
            _succeeded(command), correlation_id=command.correlation_id, causation_id=None
        )
    assert OperationCommand.objects.get(pk=command.pk).status == OperationCommand.Status.PENDING


def test_outcome_with_matching_causation_finalizes():
    command, _ = _create()
    result = commands.apply_transition_outcome(
        _succeeded(command), correlation_id=command.correlation_id,
        causation_id=command.request_event_id,
    )
    assert result.status == OperationCommand.Status.SUCCEEDED

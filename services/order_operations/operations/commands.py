"""
command creation and outcome application

operations owns only the command lifecycle and never writes storefront state. creation is
idempotent by (actor_id, command_type, target, idempotency_key). an outcome finalises a command
from any non-terminal state, including timed_out, so a command the storefront actually applied
is never left stuck, and it closes the outbox row the confirm never closed
"""

import uuid
from datetime import timedelta
from enum import StrEnum

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from event_contracts import (
    EVENT_ORDER_TRANSITION_REQUESTED,
    OrderTransitionRejectedData,
    OrderTransitionRequestedData,
    OrderTransitionSucceededData,
    TransitionRejectCode,
)

from operations import ratelimit
from operations.models import OperationAuditLog, OperationCommand, OperationsOutbox

COMMAND_TYPE_TRANSITION = "orders.transition"
REQUESTED_ROUTING_KEY = "orders.transition.requested"
ACTOR_ROLE = "restaurant_employee"
PRODUCER = "operations"
# an employee action outlives a brief broker outage: the relay's own retry schedule has to fit inside this, or a command can only ever arrive already expired
COMMAND_TTL = timedelta(minutes=5)
# a confirm advances only these; timed_out is not re-dispatched, its deadline has already
# elapsed and it stays a reconciliation signal until an outcome arrives
DISPATCHABLE = frozenset(
    {OperationCommand.Status.PENDING, OperationCommand.Status.DISPATCH_FAILED}
)
IDEMPOTENCY_KEY_MAX = 200

RESOLVABLE = frozenset(
    {OperationCommand.Status.TIMED_OUT, OperationCommand.Status.DISPATCH_FAILED}
)
MANUAL_OUTCOMES = {
    "succeeded": OperationCommand.Status.SUCCEEDED,
    "rejected": OperationCommand.Status.REJECTED,
}
MANUAL_RESULT_CODE = "resolved_manually"
# local verdicts: the broker returned the message, or the row could never form a valid envelope. neither travels the wire, so they are not outcome reject codes
CODE_UNDELIVERABLE = "undeliverable"
CODE_INVALID_PAYLOAD = "invalid_payload"
SYSTEM_ROLE = "system"
# what the employee is told. the broker's own wording belongs in the row, not in an API response
LOCAL_VERDICTS = {
    CODE_UNDELIVERABLE: "the broker never routed this command to the storefront",
    CODE_INVALID_PAYLOAD: "the command could not be encoded as a valid event",
}
RESOLUTION_REASON_MAX = 500


class CommandConflict(Exception):
    """A duplicate idempotency identity carrying a different request."""


class CommandNotResolvable(Exception):
    pass


class OutcomeMismatch(Exception):
    """An outcome envelope that does not correspond to its referenced command."""


def _request_payload(data: OrderTransitionRequestedData) -> dict:
    # intent compared on a duplicate, without server-minted or per-attempt fields
    return {
        "order_id": data.order_id,
        "expected_status": str(data.expected_status),
        "target_status": str(data.target_status),
        "reason": data.reason,
    }


def create_transition_command(
    *, actor_id: int, actor_authz_version: int, order_id: int, expected_status: str,
    target_status: str, idempotency_key: str, reason: str = "",
) -> tuple[OperationCommand, bool]:
    # validate the dedup key before any write so an oversized key is a controlled 4xx, not a
    # DataError 500 once it reaches the column
    key = (idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")
    if len(key) > IDEMPOTENCY_KEY_MAX:
        raise ValueError(f"idempotency_key must be at most {IDEMPOTENCY_KEY_MAX} characters")
    if any(ch < " " for ch in key):
        # a NUL reaches postgres as a DataError, which is the 500 this validation exists to avoid
        raise ValueError("idempotency_key must not contain control characters")
    target = str(order_id)
    command_id = uuid.uuid4()
    request_event_id = uuid.uuid4()
    # stored deadline and the one carried in the message are the same instant
    deadline = timezone.now() + COMMAND_TTL
    # validate the intent against the contract before persisting anything
    data = OrderTransitionRequestedData(
        command_id=command_id, actor_id=actor_id, actor_authz_version=actor_authz_version,
        expires_at=deadline, order_id=order_id, expected_status=expected_status,
        target_status=target_status, reason=reason,
    )
    request = _request_payload(data)

    identity = dict(
        actor_id=actor_id, command_type=COMMAND_TYPE_TRANSITION, target=target, idempotency_key=key
    )
    existing = OperationCommand.objects.filter(**identity).first()
    if existing is not None:
        return _dedup(existing, request), False
    ratelimit.charge(actor_id, order_id)

    try:
        with transaction.atomic():
            command = OperationCommand.objects.create(
                command_id=command_id, request_event_id=request_event_id, payload=request,
                deadline_at=deadline, **identity,
            )
            OperationsOutbox.objects.create(
                event_id=request_event_id,
                correlation_id=command.correlation_id,
                command=command,
                producer=PRODUCER,
                aggregate_id=target,
                aggregate_version=1,
                routing_key=REQUESTED_ROUTING_KEY,
                event_type=EVENT_ORDER_TRANSITION_REQUESTED,
                payload=data.model_dump(mode="json"),
            )
            OperationAuditLog.objects.create(
                actor_id=str(actor_id), actor_role=ACTOR_ROLE, action=COMMAND_TYPE_TRANSITION,
                target=target, command_id=command_id, result="requested",
            )
    except IntegrityError:
        # a concurrent identical create won the unique(idempotency) race
        existing = OperationCommand.objects.filter(**identity).first()
        if existing is None:
            raise
        return _dedup(existing, request), False
    return command, True


def _dedup(existing: OperationCommand, request: dict) -> OperationCommand:
    if existing.payload != request:
        raise CommandConflict("idempotency_key reused with a different request")
    return existing


def _deadline_passed(command: OperationCommand, now) -> bool:
    # a command without a deadline never expires on its own
    return command.deadline_at is not None and command.deadline_at <= now


def _settle(row: OperationsOutbox | None) -> None:
    """
    closes a still-pending row whose command already carries an outcome

    the outcome itself proves the request reached the storefront, so the attempt is finished even
    though the publisher confirm never came back. Leaving the row pending would strand it: no
    claim ever takes a row whose command is terminal, so it would hold the head of the backlog
    forever and keep the oldest-pending alert firing
    """
    if row is None or row.status != OperationsOutbox.Status.PENDING:
        return
    row.status = OperationsOutbox.Status.SETTLED
    row.locked_until = None
    row.locked_by = ""
    row.lease_token = None
    row.save(update_fields=["status", "locked_until", "locked_by", "lease_token"])


def _assert_outcome_matches_intent(command: OperationCommand, data, success: bool) -> None:
    # the outcome must describe the transition this command actually requested, so an outcome for
    # a different target (e.g. created->cancelled) can never finalise a created->confirmed command
    expected = command.payload.get("expected_status")
    target = command.payload.get("target_status")
    if success:
        if str(data.from_status) != expected or str(data.status) != target:
            raise OutcomeMismatch("succeeded outcome does not match the requested transition")
        return
    current = None if data.current_status is None else str(data.current_status)
    if data.reject_code == TransitionRejectCode.stale_status and current == expected:
        raise OutcomeMismatch("stale_status must report a status other than expected_status")
    if data.reject_code == TransitionRejectCode.invalid_transition and current != expected:
        raise OutcomeMismatch("invalid_transition must report the expected_status")


def apply_transition_outcome(data, *, correlation_id, causation_id) -> OperationCommand:
    """Finalise a command from a validated succeeded/rejected outcome; idempotent."""
    if isinstance(data, OrderTransitionSucceededData):
        success = True
    elif isinstance(data, OrderTransitionRejectedData):
        success = False
    else:
        raise OutcomeMismatch("unsupported outcome type")

    command = OperationCommand.objects.filter(command_id=data.command_id).first()
    if command is None:
        raise OutcomeMismatch("no command for outcome command_id")
    if command.command_type != COMMAND_TYPE_TRANSITION:
        raise OutcomeMismatch("command type mismatch")
    if command.target != str(data.order_id) or command.payload.get("order_id") != data.order_id:
        raise OutcomeMismatch("order_id mismatch")
    if str(command.correlation_id) != str(correlation_id):
        raise OutcomeMismatch("correlation_id mismatch")

    with transaction.atomic():
        row = OperationsOutbox.objects.select_for_update().filter(command_id=command.pk).first()
        locked = OperationCommand.objects.select_for_update().get(pk=command.pk)
        # the outcome must descend from this command's own request event, so a misrouted or forged outcome cannot finalise it
        if str(causation_id) != str(locked.request_event_id):
            raise OutcomeMismatch("causation_id does not match command request_event_id")
        _assert_outcome_matches_intent(locked, data, success)
        overruled = _manual_verdict_contradicted(locked, success)
        if locked.status in OperationCommand.TERMINAL and not overruled:
            _settle(row)  # terminal is final; the row is only closed if an earlier pass missed it
            return locked
        if success:
            locked.status = OperationCommand.Status.SUCCEEDED
            locked.result_code = ""
            locked.result_detail = ""
        else:
            locked.status = OperationCommand.Status.REJECTED
            locked.result_code = str(data.reject_code)
            locked.result_detail = data.detail
        locked.save(update_fields=["status", "result_code", "result_detail", "updated_at"])
        if overruled:
            _audit(locked, actor=locked.actor_id, role=ACTOR_ROLE, result="manual_resolution_overruled")
        _settle(row)
        return locked


class ClaimResult(StrEnum):
    CLAIMED = "claimed"          # leased by this worker, safe to publish
    BUSY = "busy"                # another worker holds an unexpired lease
    EXPIRED = "expired"          # past deadline and never sent, finalised locally
    UNAVAILABLE = "unavailable"  # already published, suppressed, or command finished


def _normalise_error(text: str, limit: int = 1000) -> str:
    """one line of printable text: an error ends up in a log line and an operator listing"""
    return " ".join(str(text).split())[:limit]


def _manual_verdict_contradicted(command: OperationCommand, success: bool) -> bool:
    """
    true when a late outcome disagrees with an operator's manual resolution

    a manual resolution is a human verdict about delivery the service could not observe, while
    the storefront is the sole writer of order state, so its outcome outranks that verdict
    """
    if command.result_code != MANUAL_RESULT_CODE:
        return False
    verdict = OperationCommand.Status.SUCCEEDED if success else OperationCommand.Status.REJECTED
    return command.status != verdict


def resolve_manually(command: OperationCommand, *, outcome: str, operator: str, reason: str) -> OperationCommand:
    """
    closes a command whose delivery never resolved, on the verdict of an operator who checked

    the service has no evidence either way here, so the decision is a human one and is recorded
    with its author and its grounds. an already closed command is refused rather than reclosed:
    a second verdict may contradict the first, and only the operator can tell
    """
    if outcome not in MANUAL_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(MANUAL_OUTCOMES)}")
    operator, reason = operator.strip(), reason.strip()
    if not operator:
        raise ValueError("an operator is required")
    if not reason:
        raise ValueError("a reason is required")
    if len(reason) > RESOLUTION_REASON_MAX:
        raise ValueError(f"reason exceeds {RESOLUTION_REASON_MAX} characters")

    with transaction.atomic():
        row = OperationsOutbox.objects.select_for_update().filter(command_id=command.pk).first()
        locked = OperationCommand.objects.select_for_update().get(pk=command.pk)
        if locked.status not in RESOLVABLE:
            raise CommandNotResolvable(
                f"{locked.command_id} is {locked.status}, only {sorted(RESOLVABLE)} may be resolved"
            )
        locked.status = MANUAL_OUTCOMES[outcome]
        locked.result_code = MANUAL_RESULT_CODE
        locked.result_detail = f"{operator}: {reason}"
        locked.save(update_fields=["status", "result_code", "result_detail", "updated_at"])
        _audit(locked, actor=operator, role="operator", result=MANUAL_RESULT_CODE)
        _settle(row)
        return locked


def reject_undeliverable(row: OperationsOutbox, *, code: str, detail: str, by: str = "commands-relay") -> OperationCommand | None:
    """
    closes a row and its command when non-delivery is established rather than suspected

    the broker refused every attempt, or the row can never form a valid envelope, so an outcome
    can never arrive and waiting for one would strand the command. the verdict is the relay's
    own and is audited as such: the employee who asked is already on the command. locks outbox
    before command, the order every other writer here uses
    """
    with transaction.atomic():
        held = _held(row)
        if held is None:
            return None
        locked_row = OperationsOutbox.objects.select_for_update().filter(**held).first()
        if locked_row is None:
            return None
        OperationsOutbox.objects.filter(pk=locked_row.pk).update(
            status=OperationsOutbox.Status.FAILED, attempts=F("attempts") + 1,
            last_error=_normalise_error(detail), next_attempt_at=None,
            locked_until=None, locked_by="", lease_token=None,
        )
        if locked_row.command_id is None:
            return None
        command = OperationCommand.objects.select_for_update().get(pk=locked_row.command_id)
        if command.status in OperationCommand.TERMINAL:
            return command
        command.status = OperationCommand.Status.REJECTED
        command.result_code = code
        command.result_detail = LOCAL_VERDICTS.get(code, code)
        command.save(update_fields=["status", "result_code", "result_detail", "updated_at"])
        _audit(command, actor=by, role=SYSTEM_ROLE, result=code)
        return command


def _suppress_expired(row: OperationsOutbox, command: OperationCommand) -> None:
    row.status = OperationsOutbox.Status.SUPPRESSED
    row.save(update_fields=["status"])
    command.status = OperationCommand.Status.REJECTED
    command.result_code = str(TransitionRejectCode.command_expired)
    command.result_detail = "expired before any publish attempt"
    command.save(update_fields=["status", "result_code", "result_detail", "updated_at"])
    _audit(command, actor=command.actor_id, role=ACTOR_ROLE, result="command_expired")


def _audit(command: OperationCommand, *, actor, role: str, result: str) -> None:
    OperationAuditLog.objects.create(
        actor_id=str(actor)[:64], actor_role=role, action=command.command_type,
        target=command.target, command_id=command.command_id, result=result,
    )


def claim_or_expire(row_id: int, *, worker: str, lease: timedelta):
    """leases one row for publishing, or finalises it when it expired before ever being sent

    locks outbox before command, the same order expire_before_dispatch uses. the attempt stamp
    and the fencing token are written here, so a row handed to the network cannot be suppressed
    and a superseded worker cannot complete it later
    """
    now = timezone.now()
    with transaction.atomic():
        row = (
            OperationsOutbox.objects.select_for_update(skip_locked=True)
            .filter(pk=row_id, status=OperationsOutbox.Status.PENDING)
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .first()
        )
        if row is None:
            still_pending = OperationsOutbox.objects.filter(
                pk=row_id, status=OperationsOutbox.Status.PENDING
            ).exists()
            return (ClaimResult.BUSY if still_pending else ClaimResult.UNAVAILABLE), None

        command = None
        if row.command_id is not None:
            command = OperationCommand.objects.select_for_update().get(pk=row.command_id)

        # the command lock can block for as long as another writer holds it; a deadline that
        # passed during that wait must not be judged by the clock read before the locks
        decision = timezone.now()
        if command is not None:
            if command.status in OperationCommand.TERMINAL:
                return ClaimResult.UNAVAILABLE, None
            if _deadline_passed(command, decision) and row.publish_attempted_at is None:
                if command.status == OperationCommand.Status.PENDING:
                    _suppress_expired(row, command)
                    return ClaimResult.EXPIRED, None
                return ClaimResult.UNAVAILABLE, None

        if row.publish_attempted_at is None:
            row.publish_attempted_at = decision
        row.lease_token = uuid.uuid4()
        row.locked_by = worker
        row.locked_until = decision + lease
        row.save(update_fields=["publish_attempted_at", "lease_token", "locked_by", "locked_until"])
        return ClaimResult.CLAIMED, row


def _held(row: OperationsOutbox) -> dict | None:
    """
    filter matching the exact live lease this worker holds, or None when it holds none

    a NULL token is not a lease: `lease_token=None` compiles to IS NULL and would match every
    unclaimed row, so completing an attempt that was never made has to be refused here
    """
    if row.lease_token is None:
        return None
    return dict(
        pk=row.pk, lease_token=row.lease_token, status=OperationsOutbox.Status.PENDING,
        locked_until__gt=timezone.now(),
    )


def confirm_dispatch(row: OperationsOutbox) -> bool:
    """
    records one publisher confirm on the outbox row and its command together

    the confirm is a single fact about two records, so it is written in one transaction. split
    across two, a relay that dies in the middle leaves a published row with a pending command:
    the row is never handed out again and the command has no way forward, so the sweeper would
    later call it timed_out even though the broker had confirmed it

    only the command advances conditionally. the confirm itself is not in doubt, so the row is
    published regardless, while a command already past its deadline stays timed_out and waits
    for an outcome
    """
    held = _held(row)
    if held is None:
        return False
    with transaction.atomic():
        locked = OperationsOutbox.objects.select_for_update().filter(**held).first()
        if locked is None:
            return False
        command = None
        if locked.command_id is not None:
            command = OperationCommand.objects.select_for_update().get(pk=locked.command_id)
        OperationsOutbox.objects.filter(pk=locked.pk).update(
            status=OperationsOutbox.Status.PUBLISHED, published_at=timezone.now(),
            attempts=F("attempts") + 1, locked_until=None, locked_by="", lease_token=None,
        )
        if command is not None and command.status in DISPATCHABLE:
            command.status = OperationCommand.Status.DISPATCHED
            command.save(update_fields=["status", "updated_at"])
        return True


def abandon_row(row: OperationsOutbox, *, error: str = "") -> bool:
    """closes a row whose publish attempts are spent

    only the row is terminal here. the attempt stamp means the message may have reached the
    storefront, so the command stays open and an outcome may still finalise it
    """
    held = _held(row)
    if held is None:
        return False
    return bool(
        OperationsOutbox.objects.filter(**held).update(
            status=OperationsOutbox.Status.FAILED, attempts=F("attempts") + 1,
            last_error=error[:1000], next_attempt_at=None,
            locked_until=None, locked_by="", lease_token=None,
        )
    )


def schedule_retry(row: OperationsOutbox, *, backoff: timedelta, error: str = "") -> bool:
    """releases the lease for a later attempt, only for the worker that still holds it"""
    held = _held(row)
    if held is None:
        return False
    return bool(
        OperationsOutbox.objects.filter(**held).update(
            attempts=F("attempts") + 1, next_attempt_at=timezone.now() + backoff,
            last_error=error[:1000], locked_until=None, locked_by="", lease_token=None,
        )
    )


def expire_before_dispatch(command: OperationCommand) -> OperationCommand:
    """sweeper entry point for a command that expired before it ever reached the broker"""
    with transaction.atomic():
        row = (
            OperationsOutbox.objects.select_for_update()
            .filter(command_id=command.pk)
            .first()
        )
        locked = OperationCommand.objects.select_for_update().get(pk=command.pk)
        # read the clock under the locks: a deadline may pass while this waits for them
        if not _deadline_passed(locked, timezone.now()):
            return locked
        if locked.status != OperationCommand.Status.PENDING:
            return locked
        if row is None or row.publish_attempted_at is not None:
            return locked
        if row.status != OperationsOutbox.Status.PENDING:
            return locked
        _suppress_expired(row, locked)
        return locked


def sweep_expired(*, limit: int = 200) -> dict[str, int]:
    """
    acts on every command whose deadline has passed

    nothing else revisits a command once the relay has let go of its outbox row, so without this
    a lost outcome leaves the command open forever and timed_out is unreachable in production
    """
    now = timezone.now()
    expiring = list(
        OperationCommand.objects.filter(
            status__in=[OperationCommand.Status.PENDING, OperationCommand.Status.DISPATCHED],
            deadline_at__lte=now,
        )
        .annotate(
            row_status=F("outbox_event__status"),
            row_attempted=F("outbox_event__publish_attempted_at"),
        )
        .order_by("deadline_at")[:limit]
    )
    swept = {"expired": 0, "timed_out": 0}
    for command in expiring:
        # the row state selected here only routes the command to one of the two paths; both still
        # re-read it under the lock, so a state that moved since the selection is resolved there
        never_sent = (
            command.status == OperationCommand.Status.PENDING
            and command.row_attempted is None
            and command.row_status == OperationsOutbox.Status.PENDING
        )
        if never_sent:
            if expire_before_dispatch(command).status == OperationCommand.Status.REJECTED:
                swept["expired"] += 1
                continue
        if mark_timed_out(command).status == OperationCommand.Status.TIMED_OUT:
            swept["timed_out"] += 1
    return swept


def _advance(command: OperationCommand, to_status, *, allowed) -> OperationCommand:
    with transaction.atomic():
        locked = OperationCommand.objects.select_for_update().get(pk=command.pk)
        if locked.status in allowed:
            locked.status = to_status
            locked.save(update_fields=["status", "updated_at"])
        return locked


def mark_timed_out(command: OperationCommand) -> OperationCommand:
    """
    deadline elapsed with delivery unresolved; NOT terminal, a late outcome may finalise it

    unresolved means two things at once: the deadline is behind us and the request actually
    reached the network. Marking a never-sent command timed_out would strand it forever, since
    the claim refuses an expired row and expiry only acts on a pending command
    """
    with transaction.atomic():
        row = OperationsOutbox.objects.select_for_update().filter(command_id=command.pk).first()
        locked = OperationCommand.objects.select_for_update().get(pk=command.pk)
        if not _deadline_passed(locked, timezone.now()):
            return locked
        if locked.status == OperationCommand.Status.PENDING:
            if row is None or row.publish_attempted_at is None:
                return locked
        elif locked.status != OperationCommand.Status.DISPATCHED:
            return locked
        locked.status = OperationCommand.Status.TIMED_OUT
        locked.save(update_fields=["status", "updated_at"])
        return locked


def mark_dispatch_failed(command: OperationCommand) -> OperationCommand:
    # the requested event exhausted its publish retries and was dead-lettered. Under
    # at-least-once delivery this does NOT prove the storefront never received it, so this is a
    # non-terminal alert/reconciliation state: a late outcome may still finalise the command.
    return _advance(
        command, OperationCommand.Status.DISPATCH_FAILED, allowed={OperationCommand.Status.PENDING}
    )

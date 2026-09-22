"""
separation of a lagging backlog from a row that will never publish

the relay retries a failed row forever, so one permanently unpublishable row kept the oldest
pending age growing and held StorefrontOutboxBacklog on, hiding any real backlog behind it
"""

import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from orders.management.commands.publish_outbox import STUCK_ATTEMPTS
from orders.management.commands.publish_outbox import Command as Relay
from orders.models import OrderOutbox

pytestmark = pytest.mark.django_db


class _Gauge:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


def _row(attempts=0, age_minutes=0, **kw):
    row = OrderOutbox.objects.create(
        aggregate_type="order",
        aggregate_id=uuid.uuid4().hex[:8], event_type="order.created",
        routing_key="order.created", payload={"order_id": 1}, attempts=attempts, **kw
    )
    if age_minutes:
        OrderOutbox.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(minutes=age_minutes)
        )
    return OrderOutbox.objects.get(pk=row.pk)


def _drain(relay=None, **gauges):
    relay = relay or Relay()
    relay._publisher.publish = Mock()
    relay._drain("w1", **gauges)


def test_a_stuck_row_no_longer_sets_the_backlog_age():
    _row(attempts=STUCK_ATTEMPTS + 1, age_minutes=120, next_attempt_at=timezone.now() + timedelta(minutes=5))
    backlog = _Gauge()

    _drain(gauge=backlog)

    assert backlog.value == 0, "a row that cannot publish must not be reported as backlog age"


def test_a_genuine_backlog_is_still_reported_behind_a_stuck_row():
    _row(attempts=STUCK_ATTEMPTS + 1, age_minutes=600, next_attempt_at=timezone.now() + timedelta(minutes=5))
    _row(attempts=0, age_minutes=30, next_attempt_at=timezone.now() + timedelta(minutes=5))
    backlog = _Gauge()

    _drain(gauge=backlog)

    assert 1500 < backlog.value < 2100, f"expected the 30-minute row to set the age, got {backlog.value}"


def test_stuck_rows_are_counted_as_their_own_signal():
    _row(attempts=STUCK_ATTEMPTS + 1, next_attempt_at=timezone.now() + timedelta(minutes=5))
    _row(attempts=STUCK_ATTEMPTS + 4, next_attempt_at=timezone.now() + timedelta(minutes=5))
    _row(attempts=1, next_attempt_at=timezone.now() + timedelta(minutes=5))
    stuck = _Gauge()

    _drain(stuck_gauge=stuck)

    assert stuck.value == 2


def test_a_row_short_of_the_threshold_still_counts_as_backlog():
    _row(attempts=STUCK_ATTEMPTS - 1, age_minutes=45, next_attempt_at=timezone.now() + timedelta(minutes=5))
    backlog, stuck = _Gauge(), _Gauge()

    _drain(gauge=backlog, stuck_gauge=stuck)

    assert stuck.value == 0
    assert backlog.value > 2400


def test_the_relay_never_abandons_a_row_on_its_own():
    row = _row(attempts=STUCK_ATTEMPTS + 50, next_attempt_at=timezone.now() + timedelta(minutes=5))

    _drain()

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING, "a domain event is never dropped automatically"


def test_the_operator_can_see_only_the_rows_that_are_stuck(capsys):
    stuck = _row(attempts=STUCK_ATTEMPTS + 1)
    _row(attempts=1)

    call_command("outbox", "--list")

    out = capsys.readouterr().out
    assert f"#{stuck.pk}" in out
    assert "1 row(s)" in out


def test_retry_puts_a_stuck_row_back_in_the_running():
    row = _row(attempts=STUCK_ATTEMPTS + 5, next_attempt_at=timezone.now() + timedelta(hours=1))
    OrderOutbox.objects.filter(pk=row.pk).update(locked_by="dead-worker", lease_token=uuid.uuid4())

    call_command("outbox", "--retry", str(row.pk))

    row.refresh_from_db()
    assert (row.attempts, row.next_attempt_at, row.lease_token, row.locked_by) == (0, None, None, "")
    assert row.status == OrderOutbox.Status.PENDING


def test_closing_a_row_needs_both_gates():
    row = _row(attempts=STUCK_ATTEMPTS + 1)

    with pytest.raises(CommandError):
        call_command("outbox", "--discard", str(row.pk))
    with pytest.raises(CommandError):
        call_command("outbox", "--discard", str(row.pk), "--yes")

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PENDING


def test_a_closed_row_is_terminal_and_leaves_a_trace(caplog):
    row = _row(attempts=STUCK_ATTEMPTS + 1)

    call_command("outbox", "--discard", str(row.pk), "--yes",
                 "--reason", "routing key retired", "--operator", "ops-7")

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.FAILED
    assert "ops-7" in row.last_error
    assert any("outbox row closed by an operator" in r.message for r in caplog.records)


def test_a_closed_row_is_never_claimed_again():
    row = _row(attempts=STUCK_ATTEMPTS + 1)
    call_command("outbox", "--discard", str(row.pk), "--yes", "--reason", "x", "--operator", "ops-7")

    assert Relay()._claim("w1") == []


def test_a_published_row_cannot_be_closed():
    row = _row()
    OrderOutbox.objects.filter(pk=row.pk).update(status=OrderOutbox.Status.PUBLISHED)

    call_command("outbox", "--discard", str(row.pk), "--yes", "--reason", "x", "--operator", "ops-7")

    row.refresh_from_db()
    assert row.status == OrderOutbox.Status.PUBLISHED

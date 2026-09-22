import logging
import os
import random
import socket
import time
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from event_contracts import (
    EVENT_ORDER_TRANSITION_REJECTED,
    EVENT_ORDER_TRANSITION_SUCCEEDED,
)

from config.jsonlog import describe_error, log_context
from orders import messaging
from orders.models import OrderOutbox

try:
    from prometheus_client import Gauge, start_http_server
except ImportError:  # pragma: no cover
    Gauge = None
    start_http_server = None

log = logging.getLogger(__name__)

BATCH = 50
LEASE_SECONDS = 60
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 300
STUCK_ATTEMPTS = 12


def _backoff_seconds(attempts: int) -> float:
    """Exponential backoff (capped) with additive jitter."""
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(attempts, 10)))
    return base + random.uniform(0, base * 0.25)


# events the storefront already emits in contract shape; their body carries the whole envelope, so nothing downstream has to rebuild it from headers
ENVELOPE_EVENTS = frozenset({EVENT_ORDER_TRANSITION_SUCCEEDED, EVENT_ORDER_TRANSITION_REJECTED})
PRODUCER = "storefront"


def _body(row: OrderOutbox) -> dict:
    """assembled from typed columns; the stored payload is only the contract `data`"""
    if row.event_type not in ENVELOPE_EVENTS:
        return row.payload
    return {
        "event_id": str(row.event_id),
        "event_type": row.event_type,
        "schema_version": row.schema_version,
        "occurred_at": row.created_at.isoformat(),
        "producer": PRODUCER,
        "aggregate": {
            "type": row.aggregate_type,
            "id": row.aggregate_id,
            "version": row.aggregate_version,
        },
        "correlation_id": str(row.correlation_id),
        "causation_id": str(row.causation_id) if row.causation_id else None,
        "data": row.payload,
    }


def _held(row: OrderOutbox) -> dict | None:
    """
    filter matching the live lease this worker holds, or None when it holds none

    null token is not a lease: `lease_token=None` compiles to IS NULL and would match every
    unclaimed row
    """
    if row.lease_token is None:
        return None
    return dict(
        pk=row.pk, lease_token=row.lease_token, status=OrderOutbox.Status.PENDING,
        locked_until__gt=timezone.now(),
    )


def _headers(row: OrderOutbox) -> dict:
    return {
        "event_id": str(row.event_id),
        "correlation_id": str(row.correlation_id),
        "causation_id": str(row.causation_id) if row.causation_id else None,
        "schema_version": row.schema_version,
        "aggregate_version": row.aggregate_version,
        # domain occurred_at travels with the event so downstream never re-times it
        "occurred_at": row.created_at.isoformat(),
        "snapshot": row.snapshot,
        "snapshot_run_id": row.snapshot_run_id or None,
    }


class Command(BaseCommand):
    help = "Leased at-least-once outbox relay: publishes pending order events to RabbitMQ."

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._publisher = messaging.Publisher()

    def add_arguments(self, parser) -> None:
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=float, default=1.0)
        parser.add_argument(
            "--metrics-port", type=int, default=int(os.environ.get("OUTBOX_METRICS_PORT", "0"))
        )

    def handle(self, *args, **options) -> None:
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        gauge = stuck_gauge = None
        if options["metrics_port"] and start_http_server is not None:
            start_http_server(options["metrics_port"])
            gauge = Gauge("outbox_oldest_pending_age_seconds", "age of the oldest pending outbox event")
            stuck_gauge = Gauge("outbox_stuck_rows", "pending outbox rows past their attempt threshold")

        self._converge_topology()

        try:
            if options["loop"]:
                self.stdout.write(self.style.SUCCESS(f"outbox relay {worker_id} started"))
                while True:
                    # an aggregate publishes one event per claim, so a claim that found work is followed at once
                    if not self._drain(worker_id, gauge, stuck_gauge):
                        time.sleep(options["interval"])
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"published {self._drain(worker_id, gauge, stuck_gauge)} event(s)"
                    )
                )
        finally:
            self._publisher.close()

    def _converge_topology(self) -> None:
        conn = messaging.connect()
        try:
            channel = conn.channel()
            messaging.declare_topology(channel)
        finally:
            conn.close()

    def _claim(self, worker_id: str) -> list[OrderOutbox]:
        now = timezone.now()
        eligible = (
            Q(status=OrderOutbox.Status.PENDING)
            & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            & (Q(locked_until__isnull=True) | Q(locked_until__lt=now))
        )
        # consumers apply an aggregate's events in version order, so only its oldest unsent event may go:
        # a later one waits out the earlier one's backoff and lease, and a closed one until an operator acts
        earlier = OrderOutbox.objects.filter(
            status__in=(OrderOutbox.Status.PENDING, OrderOutbox.Status.FAILED),
            aggregate_type=OuterRef("aggregate_type"),
            aggregate_id=OuterRef("aggregate_id"),
            id__lt=OuterRef("id"),
        )
        with transaction.atomic():
            rows = list(
                OrderOutbox.objects.select_for_update(skip_locked=True)
                .filter(eligible)
                .exclude(Exists(earlier))
                .order_by("created_at")[:BATCH]
            )
            if rows:
                # one token per claim: a re-claim mints a new one, so the previous holder is
                # fenced off that row while keeping its own other rows
                token = uuid.uuid4()
                locked_until = now + timedelta(seconds=LEASE_SECONDS)
                OrderOutbox.objects.filter(pk__in=[r.pk for r in rows]).update(
                    locked_until=locked_until, locked_by=worker_id, lease_token=token
                )
                for row in rows:
                    row.lease_token, row.locked_until, row.locked_by = token, locked_until, worker_id
        return rows

    def _drain(self, worker_id: str, gauge=None, stuck_gauge=None) -> int:
        rows = self._claim(worker_id)
        for row in rows:
            self._publish_one(worker_id, row)
        pending = OrderOutbox.objects.filter(status=OrderOutbox.Status.PENDING)
        if gauge is not None:
            oldest = (
                pending.filter(attempts__lt=STUCK_ATTEMPTS)
                .order_by("created_at")
                .values_list("created_at", flat=True)
                .first()
            )
            gauge.set((timezone.now() - oldest).total_seconds() if oldest else 0.0)
        if stuck_gauge is not None:
            stuck_gauge.set(pending.filter(attempts__gte=STUCK_ATTEMPTS).count())
        return len(rows)

    def _publish_one(self, worker_id: str, row: OrderOutbox) -> None:
        with log_context(
            event_id=str(row.event_id),
            correlation_id=str(row.correlation_id),
            causation_id=str(row.causation_id) if row.causation_id else None,
            event_type=row.event_type,
        ):
            self._publish_held(worker_id, row)

    def _publish_held(self, worker_id: str, row: OrderOutbox) -> None:
        held = _held(row)
        if held is None:
            return
        now = timezone.now()
        try:
            self._publisher.publish(row.routing_key, row.event_type, _body(row), _headers(row))
        except Exception as exc:  # broker down / unconfirmed -> keep pending, backoff
            attempts = row.attempts + 1
            OrderOutbox.objects.filter(**held).update(
                attempts=attempts,
                next_attempt_at=now + timedelta(seconds=_backoff_seconds(attempts)),
                last_error=describe_error(exc),
                locked_until=None,
                locked_by="",
                lease_token=None,
            )
            log.exception(
                "outbox publish failed",
                extra={"outbox_id": row.pk, "routing_key": row.routing_key, "attempts": attempts},
            )
            return
        OrderOutbox.objects.filter(**held).update(
            status=OrderOutbox.Status.PUBLISHED,
            published_at=now,
            attempts=row.attempts + 1,
            locked_until=None,
            locked_by="",
            lease_token=None,
        )

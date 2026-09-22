"""
operator handling of outbox rows the relay cannot publish

a domain event is never dropped automatically, so a row that keeps failing stays pending and
needs a human: either the cause is fixed and the row is retried, or the row is closed on purpose.
a closed row keeps holding the later events of its aggregate, which consumers apply in order,
until it is retried or the aggregate is resynced: its unsent history replaced with its current state
"""

import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from orders.management.commands.publish_outbox import STUCK_ATTEMPTS
from orders.models import Order, OrderOutbox, OrderStatusHistory

log = logging.getLogger(__name__)

ERROR_PREVIEW = 160
UNSENT = (OrderOutbox.Status.PENDING, OrderOutbox.Status.FAILED)
RELEASED_LEASE = {"next_attempt_at": None, "locked_until": None, "locked_by": "", "lease_token": None}


def _stuck():
    return OrderOutbox.objects.filter(
        status=OrderOutbox.Status.PENDING, attempts__gte=STUCK_ATTEMPTS
    ).order_by("created_at")


def _held_by(row) -> int:
    return OrderOutbox.objects.filter(
        status=OrderOutbox.Status.PENDING,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        id__gt=row.id,
    ).count()


def _holders():
    for row in OrderOutbox.objects.filter(status=OrderOutbox.Status.FAILED).order_by("created_at"):
        held = _held_by(row)
        if held:
            yield row, held


def _number(aggregate_id: str) -> int:
    if not aggregate_id.isdigit():
        raise CommandError(f"{aggregate_id!r} is not the id of this aggregate type")
    return int(aggregate_id)


def _source(aggregate_type: str, aggregate_id: str):
    """locks the row an aggregate lives in; returns its current version and an emitter of its whole state"""
    if aggregate_type == OrderOutbox.AggregateType.ORDER:
        from orders import service as order_service

        order = Order.objects.select_for_update().filter(pk=_number(aggregate_id)).first()
        if order is None:
            return None
        version = OrderStatusHistory.objects.filter(order=order).count()
        return version, lambda: order_service.emit_order_state(order)
    if aggregate_type == OrderOutbox.AggregateType.PRODUCT:
        from menu import inventory
        from menu.models import Product

        product = Product.objects.select_for_update().filter(code=aggregate_id).first()
        if product is None:
            return None
        return product.version, lambda: inventory.emit_state(product)
    user = get_user_model().objects.select_for_update().filter(pk=_number(aggregate_id)).first()
    if user is None:
        return None
    if aggregate_type == OrderOutbox.AggregateType.CUSTOMER:
        from accounts import service as accounts_service

        return user.customer_version, lambda: accounts_service.emit_state(user)
    from employee import service as employee_service

    return user.authz_version, lambda: employee_service.emit_authz(user)


class Command(BaseCommand):
    help = (
        "Lists outbox rows the relay cannot publish and closed rows holding their aggregate, "
        "retries or closes rows, or resyncs an aggregate from its current state."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--retry", nargs="+", type=int, default=[], metavar="ID")
        parser.add_argument("--discard", nargs="+", type=int, default=[], metavar="ID")
        parser.add_argument(
            "--resync",
            default="",
            metavar="TYPE:ID",
            help="replace the unsent events of one aggregate with its current state",
        )
        parser.add_argument("--yes", action="store_true", help="required to close or resync")
        parser.add_argument("--reason", default="", help="why the rows will never be published")
        parser.add_argument("--operator", default="", help="who is taking the action")

    def handle(self, *args, **options) -> None:
        retry, discard, resync = options["retry"], options["discard"], options["resync"]
        if sum(map(bool, (retry, discard, resync))) > 1:
            raise CommandError("retry, discard and resync cannot run together")
        if discard or resync:
            if not options["yes"]:
                raise CommandError("closing or resyncing needs --yes")
            if not options["reason"].strip():
                raise CommandError("closing or resyncing needs --reason")
        if retry:
            self._retry(retry)
        elif discard:
            self._discard(discard, options["reason"], options["operator"])
        elif resync:
            self._resync(resync, options["reason"], options["operator"])
        else:
            self._list()

    def _list(self) -> None:
        rows = list(_stuck())
        self.stdout.write(f"{len(rows)} row(s) past {STUCK_ATTEMPTS} attempts")
        now = timezone.now()
        for row in rows:
            age = int((now - row.created_at).total_seconds())
            self.stdout.write(
                f"  #{row.pk} {row.event_type} -> {row.routing_key} | "
                f"attempts={row.attempts} age={age}s"
            )
            if row.last_error:
                self.stdout.write(f"    {row.last_error[:ERROR_PREVIEW]}")
        holders = list(_holders())
        self.stdout.write(f"{len(holders)} closed row(s) holding their aggregate")
        for row, held in holders:
            self.stdout.write(
                f"  #{row.pk} {row.event_type} {row.aggregate_type}:{row.aggregate_id} | "
                f"holds {held} later event(s): --retry {row.pk} or "
                f"--resync {row.aggregate_type}:{row.aggregate_id}"
            )

    def _retry(self, ids) -> None:
        # the lease is cleared too, so a row held by a worker that died is claimable again, and a closed row
        # is reopened in its place in the aggregate
        updated = OrderOutbox.objects.filter(pk__in=ids, status__in=UNSENT).update(
            status=OrderOutbox.Status.PENDING, attempts=0, **RELEASED_LEASE
        )
        for row_id in ids:
            log.info("outbox row queued for another attempt", extra={"outbox_id": row_id})
        self.stdout.write(self.style.SUCCESS(f"{updated} row(s) will be attempted again"))

    def _discard(self, ids, reason: str, operator: str) -> None:
        rows = list(OrderOutbox.objects.filter(pk__in=ids, status=OrderOutbox.Status.PENDING))
        for row in rows:
            log.warning(
                "outbox row closed by an operator",
                extra={
                    "outbox_id": row.pk,
                    "event_id": str(row.event_id),
                    "event_type": row.event_type,
                    "routing_key": row.routing_key,
                    "operator": operator or "unattributed",
                    "reason": reason,
                },
            )
        updated = OrderOutbox.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=OrderOutbox.Status.FAILED,
            last_error=f"closed by {operator or 'unattributed'}: {reason}"[:1000],
            **RELEASED_LEASE,
        )
        self.stdout.write(self.style.SUCCESS(f"{updated} row(s) closed"))
        for row in rows:
            held = _held_by(row)
            if held:
                self.stdout.write(
                    f"  #{row.pk} holds {held} later event(s) of {row.aggregate_type}:{row.aggregate_id} "
                    f"until --retry {row.pk} or --resync {row.aggregate_type}:{row.aggregate_id}"
                )

    def _resync(self, target: str, reason: str, operator: str) -> None:
        aggregate_type, _, aggregate_id = target.partition(":")
        if aggregate_type not in OrderOutbox.AggregateType.values or not aggregate_id:
            raise CommandError(
                f"--resync takes TYPE:ID, TYPE one of {', '.join(OrderOutbox.AggregateType.values)}"
            )
        with transaction.atomic():
            # the aggregate's own row first, as its writers lock it, so no event of it is written meanwhile
            source = None
            if aggregate_type != OrderOutbox.AggregateType.SNAPSHOT:
                source = _source(aggregate_type, aggregate_id)
                if source is None:
                    raise CommandError(f"{target} does not exist")
            rows = list(
                OrderOutbox.objects.select_for_update()
                .filter(aggregate_type=aggregate_type, aggregate_id=aggregate_id, status__in=UNSENT)
                .order_by("id")
            )
            if not rows:
                raise CommandError(
                    f"{target} has no unsent events, its consumers hold its latest state"
                )
            now = timezone.now()
            if any(row.locked_until and row.locked_until > now for row in rows):
                raise CommandError(
                    f"a relay is publishing an event of {target} right now, run again"
                )
            if source is not None:
                version, emit = source
                published = OrderOutbox.objects.filter(
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    status=OrderOutbox.Status.PUBLISHED,
                    aggregate_version__gte=version,
                )
                # a consumer at this version refuses the same version again
                if published.exists():
                    raise CommandError(f"{target} version {version} was already published")
            OrderOutbox.objects.filter(pk__in=[row.pk for row in rows]).update(
                status=OrderOutbox.Status.SUPERSEDED,
                last_error=f"superseded by {operator or 'unattributed'}: {reason}"[:1000],
                **RELEASED_LEASE,
            )
            if source is not None:
                emit()
        log.warning(
            "outbox aggregate resynced by an operator",
            extra={
                "aggregate": target,
                "superseded": len(rows),
                "operator": operator or "unattributed",
                "reason": reason,
            },
        )
        outcome = (
            "with its current state" if source is not None else "and its snapshot run abandoned"
        )
        self.stdout.write(
            self.style.SUCCESS(f"{len(rows)} unsent event(s) of {target} replaced {outcome}")
        )

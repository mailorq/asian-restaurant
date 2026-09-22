"""
removes the legacy in-storefront projection queue from a broker

orders.ops is bound to order events with phones and addresses and has no consumer in production, so it only grows.
unbinding stops the growth at once; deleting waits until the backlog was looked at
"""

from django.core.management.base import BaseCommand, CommandError
from pika.exceptions import ChannelClosedByBroker

from orders import messaging

QUEUES = (messaging.OPS_QUEUE, messaging.RETRY_QUEUE, messaging.OPS_DLQ)
EXCHANGES = (messaging.RETRY_EXCHANGE, messaging.DLX)
# the wildcard is what releases before the enumerated keys bound
BINDINGS = (*messaging.LEGACY_PROJECTION_KEYS, "order.*")
NOT_FOUND = 404


def _depth(connection, queue: str) -> int | None:
    channel = connection.channel()
    try:
        return channel.queue_declare(queue=queue, passive=True).method.message_count
    except ChannelClosedByBroker as exc:
        if exc.reply_code == NOT_FOUND:
            return None
        raise
    finally:
        if channel.is_open:
            channel.close()


class Command(BaseCommand):
    help = (
        "Unbind the legacy orders.ops queue from order events and delete it with its retry and dead letter queues. "
        "Stops at a backlog and names it; --discard-backlog deletes the queues with what is in them."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--discard-backlog",
            action="store_true",
            help="delete the queues although they still hold messages",
        )

    def handle(self, *args, discard_backlog: bool, **options) -> None:
        connection = messaging.connect()
        try:
            depths = {queue: _depth(connection, queue) for queue in QUEUES}
            present = {queue: depth for queue, depth in depths.items() if depth is not None}
            if not present:
                self.stdout.write("nothing to retire: the legacy queues are gone")
                return
            channel = connection.channel()
            if messaging.OPS_QUEUE in present:
                for key in BINDINGS:
                    channel.queue_unbind(
                        queue=messaging.OPS_QUEUE, exchange=messaging.EXCHANGE, routing_key=key
                    )
                self.stdout.write(f"{messaging.OPS_QUEUE} no longer receives order events")
            backlog = {queue: depth for queue, depth in present.items() if depth}
            if backlog and not discard_backlog:
                listed = ", ".join(f"{queue}: {depth}" for queue, depth in backlog.items())
                raise CommandError(
                    f"left in place, messages remain ({listed}). They are order events with customer data that "
                    f"nothing reads; after checking them, run again with --discard-backlog"
                )
            for queue in present:
                channel.queue_delete(queue=queue, if_empty=not discard_backlog)
            for exchange in EXCHANGES:
                channel.exchange_delete(exchange=exchange, if_unused=True)
            discarded = sum(backlog.values())
            self.stdout.write(
                self.style.SUCCESS(
                    f"retired {', '.join(present)}"
                    + (f", discarding {discarded} messages" if discarded else "")
                )
            )
        finally:
            if connection.is_open:
                connection.close()

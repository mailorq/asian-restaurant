"""
operator handling of the operations dead-letter queues

replay goes through the exchange with the original routing key: only the queues the message fell out of are bound
to it for that key, so it lands back where it was. the bridge queues live on the storefront vhost, which only the
bridge credentials reach, so `--queue bridge` runs in the operations-bridge container
"""

import datetime as dt
import hashlib
import json
import logging

import pika
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from operations import messaging
from operations.management.commands import bridge_storefront_events as bridge

log = logging.getLogger(__name__)

# dead-letter queue and the exchange its replay goes through
FLOWS = {
    "projection": (messaging.DLQ, messaging.EXCHANGE),
    "bridge": (bridge.BRIDGE_DLQ, bridge.BRIDGE_REQUEUE_EXCHANGE),
}
# what a spent attempt leaves on a message; a replay starts with the whole retry budget
SPENT = (
    "x-retries",
    bridge.RETRY_HEADER,
    "x-death",
    "x-first-death-reason",
    "x-first-death-queue",
    "x-first-death-exchange",
)
PREVIEW = 20
DIGEST = 16


def _bridge_connect() -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(settings.BRIDGE_CONSUME_URL))


def _digest(body: bytes) -> str:
    """identifies a payload across log lines without carrying what is inside it"""
    return hashlib.sha256(body).hexdigest()[:DIGEST]


def _summary(properties, body: bytes) -> str:
    try:
        # a storefront event before the bridge carries its type on the message, not in the body
        event_type = json.loads(body).get("event_type") or properties.type or "?"
    except (json.JSONDecodeError, AttributeError, TypeError):
        event_type = "unparsed"
    death = (properties.headers or {}).get("x-death") or [{}]
    return (f"{event_type} | reason={death[0].get('reason', '?')} | id={properties.message_id}"
            f" | sha256={_digest(body)} | {len(body)}B")


class Command(BaseCommand):
    help = "Inspects an operations dead-letter queue, replays messages, or discards them."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--queue", choices=sorted(FLOWS), default="projection")
        parser.add_argument("--list", action="store_true", help="show what is queued, consuming nothing")
        parser.add_argument("--replay", type=int, default=0, metavar="N")
        parser.add_argument("--drop", type=int, default=0, metavar="N")
        parser.add_argument("--yes", action="store_true", help="required to discard")
        parser.add_argument("--reason", default="", help="why the discarded messages are unprocessable")
        parser.add_argument("--operator", default="", help="who is taking the action")
        parser.add_argument("--show-payload", action="store_true",
                            help="print message bodies; they carry customer phone and address")

    def handle(self, *args, **options) -> None:
        replay, drop = options["replay"], options["drop"]
        if replay and drop:
            raise CommandError("replay and drop cannot run together")
        if drop:
            if not options["yes"]:
                raise CommandError("discarding needs --yes")
            if not options["reason"].strip():
                raise CommandError("discarding needs --reason")
        if not (replay or drop):
            options["list"] = True

        queue, exchange = FLOWS[options["queue"]]
        connection = _bridge_connect() if options["queue"] == "bridge" else messaging.connect()
        channel = connection.channel()
        channel.confirm_delivery()
        try:
            if options["list"]:
                self._list(channel, queue, options["show_payload"])
            elif replay:
                self._replay(channel, queue, exchange, replay, options["operator"])
            else:
                self._drop(channel, queue, drop, options["reason"], options["operator"])
        finally:
            connection.close()

    def _list(self, channel, queue: str, show_payload: bool = False) -> None:
        """reads without consuming: everything is put back before the command returns"""
        held = []
        while len(held) < PREVIEW:
            method, properties, body = channel.basic_get(queue, auto_ack=False)
            if method is None:
                break
            held.append((method, properties, body))
        self.stdout.write(f"{queue}: {len(held)} message(s) read")
        for _, properties, body in held:
            self.stdout.write(f"  {_summary(properties, body)}")
            if show_payload:
                self.stdout.write(f"    {body.decode(errors='replace')}")
        for method, _, _ in held:
            channel.basic_nack(method.delivery_tag, requeue=True)

    def _replay(self, channel, queue: str, exchange: str, count: int, operator: str) -> None:
        done = 0
        while done < count:
            method, properties, body = channel.basic_get(queue, auto_ack=False)
            if method is None:
                break
            headers = {k: v for k, v in (properties.headers or {}).items() if k not in SPENT}
            headers["x-replayed-at"] = dt.datetime.now(dt.UTC).isoformat()
            headers["x-replayed-by"] = operator or "unattributed"
            try:
                channel.basic_publish(
                    exchange=exchange,
                    routing_key=method.routing_key,
                    body=body,
                    # the bridge maps an event by its type, so the type travels with the replay
                    properties=pika.BasicProperties(
                        content_type=properties.content_type or "application/json",
                        delivery_mode=2,
                        message_id=properties.message_id,
                        correlation_id=properties.correlation_id,
                        type=properties.type,
                        headers=headers,
                    ),
                    mandatory=True,
                )
            except Exception as exc:
                # the message is only ever removed after a confirm, so it stays put on failure
                channel.basic_nack(method.delivery_tag, requeue=True)
                raise CommandError(f"replay stopped after {done}: {exc}") from exc
            channel.basic_ack(method.delivery_tag)
            done += 1
            log.info("dlq message replayed", extra={"message_id": properties.message_id,
                                                    "routing_key": method.routing_key})
        self.stdout.write(self.style.SUCCESS(f"replayed {done} message(s) to {exchange}"))

    def _drop(self, channel, queue: str, count: int, reason: str, operator: str) -> None:
        done = 0
        while done < count:
            method, properties, body = channel.basic_get(queue, auto_ack=False)
            if method is None:
                break
            log.warning(
                "dlq message discarded",
                extra={"message_id": properties.message_id, "routing_key": method.routing_key,
                       "operator": operator or "unattributed", "reason": reason,
                       "body_sha256": _digest(body), "body_bytes": len(body)},
            )
            channel.basic_ack(method.delivery_tag)
            done += 1
        self.stdout.write(self.style.SUCCESS(f"discarded {done} message(s) from {queue}"))

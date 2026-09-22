import json
import logging
import uuid

from django.core.management.base import BaseCommand
from django.db import DatabaseError

from config.jsonlog import describe_error
from ops import service
from orders import messaging

log = logging.getLogger(__name__)

KNOWN_EVENT_TYPES = {"order.created", "order.status_changed"}
SUPPORTED_SCHEMA = 1


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _valid_uuid(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


class Command(BaseCommand):
    help = "Consume order events from RabbitMQ into the RestaurantOrder projection (ops runtime)."

    def handle(self, *args, **options) -> None:
        connection = messaging.connect()
        channel = connection.channel()
        messaging.declare_legacy_topology(channel)
        messaging.converge_legacy_binding(channel)
        channel.confirm_delivery()  # retry re publishes must be broker confirmed
        channel.basic_qos(prefetch_count=10)
        channel.basic_consume(queue=messaging.OPS_QUEUE, on_message_callback=self._on_message)
        self.stdout.write(self.style.SUCCESS(f"ops consumer listening on {messaging.OPS_QUEUE}"))
        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            channel.stop_consuming()
        finally:
            connection.close()

    def _on_message(self, channel, method, properties, body) -> None:
        headers = properties.headers or {}
        retries = _safe_int(headers.get("x-retries"), 0)
        event_type = properties.type
        event_id = headers.get("event_id") or properties.message_id
        aggregate_version = headers.get("aggregate_version")
        correlation_id = headers.get("correlation_id") or getattr(properties, "correlation_id", None)

        # strict envelope validation — never dedup on "None", never coerce a bad
        # aggregate_version to 0; anything unusable is poison -> DLQ, no crash.
        if (
            event_type not in KNOWN_EVENT_TYPES
            or _safe_int(headers.get("schema_version")) != SUPPORTED_SCHEMA
            or not _valid_uuid(event_id)
            or not isinstance(aggregate_version, int)
            or aggregate_version < 1
            or not _valid_uuid(correlation_id)
        ):
            log.warning(
                "ops invalid envelope: type=%r schema=%r event_id=%r agg_v=%r corr=%r -> DLQ",
                event_type, headers.get("schema_version"), event_id, aggregate_version, correlation_id,
            )
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("ops poison (bad json) -> DLQ", extra={"message_id": getattr(properties, "message_id", None), "error_shape": describe_error(exc)})
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        if not isinstance(payload, dict) or "order_id" not in payload:
            log.warning("ops poison (missing order_id) -> DLQ")
            channel.basic_nack(method.delivery_tag, requeue=False)
            return

        try:
            service.apply_event(
                event_id=event_id,
                event_type=event_type,
                aggregate_version=aggregate_version,
                payload=payload,
            )
        except (KeyError, TypeError) as exc:
            # the rejected value is the event body, which carries customer data
            log.warning("ops poison (bad payload) -> DLQ", extra={"event_id": event_id, "event_type": event_type, "error_shape": describe_error(exc)})
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        except (service.OutOfOrder, DatabaseError):
            self._retry(channel, method, properties, payload, retries)
            return

        channel.basic_ack(method.delivery_tag)

    def _retry(self, channel, method, properties, payload, retries: int) -> None:
        if retries >= messaging.MAX_RETRIES:
            log.warning("ops retries exhausted (%s) -> DLQ", messaging.MAX_RETRIES)
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        headers = {**(properties.headers or {}), "x-retries": retries + 1}
        try:
            messaging.publish_message(
                channel, messaging.RETRY_EXCHANGE, method.routing_key, properties.type, payload, headers
            )
        except Exception:
            # retry publish not confirmed by the broker do not lose the original
            log.exception("ops retry publish failed; requeueing original")
            channel.basic_nack(method.delivery_tag, requeue=True)
            return
        channel.basic_ack(method.delivery_tag)  # only after the retry is confirmed

import json
import logging
from decimal import Decimal

import pika
from django.conf import settings
from django.core.management.base import BaseCommand
from event_contracts import (
    EVENT_AUTHZ_CHANGED,
    EVENT_CUSTOMER_CHANGED,
    EVENT_ORDER_CREATED,
    EVENT_ORDER_STATUS_CHANGED,
    EVENT_ORDER_TRANSITION_REJECTED,
    EVENT_ORDER_TRANSITION_SUCCEEDED,
    EVENT_SNAPSHOT_CONTROL,
    EVENT_STOCK_CHANGED,
    parse_event,
)
from pika.exceptions import UnroutableError

from operations import messaging
from operations.jsonlog import describe_error, log_context

log = logging.getLogger(__name__)

# temporary adapter: storefront still emits legacy `order.*`; remove once storefront
# publishes versioned envelopes natively. it spans two vhosts — consumes storefront,
# publishes operations — so it never shares a connection or credentials with either app.
STOREFRONT_EXCHANGE = "orders"
BRIDGE_QUEUE = "operations.bridge.storefront"
BRIDGE_OUTCOME_QUEUE = "operations.bridge.outcomes"
BRIDGE_DLX = "operations.bridge.dlx"
BRIDGE_DLQ = "operations.bridge.storefront.dlq"
BRIDGE_RETRY_EXCHANGE = "operations.bridge.retry"
BRIDGE_RETRY_QUEUE = "operations.bridge.storefront.retry"
BRIDGE_REQUEUE_EXCHANGE = "operations.bridge.requeue"
BRIDGE_RETRY_TTL_MS = 5000
BRIDGE_MAX_RETRIES = 5
RETRY_HEADER = "x-bridge-retries"

# storefront routing keys, split by queue. outcomes carry the command plane and must not queue behind a projection backfill, so `order.*` is enumerated rather than matched
LEGACY_KEYS = ("order.created", "order.status_changed", "inventory.*", "identity.*", "snapshot.*")
OUTCOME_KEYS = ("order.transition_succeeded", "order.transition_rejected")
CONSUMED_QUEUES = (BRIDGE_QUEUE, BRIDGE_OUTCOME_QUEUE)

LEGACY_TO_VERSIONED = {
    "order.created": EVENT_ORDER_CREATED,
    "order.status_changed": EVENT_ORDER_STATUS_CHANGED,
    "inventory.stock_changed": EVENT_STOCK_CHANGED,
    "identity.customer_changed": EVENT_CUSTOMER_CHANGED,
    "identity.authz_changed": EVENT_AUTHZ_CHANGED,
    "snapshot.control": EVENT_SNAPSHOT_CONTROL,
    "orders.transition.succeeded.v1": EVENT_ORDER_TRANSITION_SUCCEEDED,
    "orders.transition.rejected.v1": EVENT_ORDER_TRANSITION_REJECTED,
}

# aggregate type + the legacy payload key that holds the aggregate id
_AGGREGATE = {
    EVENT_ORDER_CREATED: ("order", "order_id"),
    EVENT_ORDER_STATUS_CHANGED: ("order", "order_id"),
    EVENT_STOCK_CHANGED: ("product", "product_code"),
    EVENT_CUSTOMER_CHANGED: ("customer", "customer_id"),
    EVENT_AUTHZ_CHANGED: ("authz", "subject_id"),
    EVENT_SNAPSHOT_CONTROL: ("snapshot", "run_id"),
    EVENT_ORDER_TRANSITION_SUCCEEDED: ("order", "order_id"),
    EVENT_ORDER_TRANSITION_REJECTED: ("order", "order_id"),
}


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def declare_bridge_topology(channel) -> None:
    # storefront owns the `orders` exchange; the bridge only binds to it (read perm),
    # so it must not declare it here
    channel.exchange_declare(exchange=BRIDGE_DLX, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=BRIDGE_RETRY_EXCHANGE, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=BRIDGE_REQUEUE_EXCHANGE, exchange_type="topic", durable=True)

    channel.queue_declare(queue=BRIDGE_DLQ, durable=True)
    channel.queue_bind(queue=BRIDGE_DLQ, exchange=BRIDGE_DLX, routing_key="#")

    channel.queue_declare(
        queue=BRIDGE_RETRY_QUEUE,
        durable=True,
        arguments={"x-message-ttl": BRIDGE_RETRY_TTL_MS, "x-dead-letter-exchange": BRIDGE_REQUEUE_EXCHANGE},
    )
    channel.queue_bind(queue=BRIDGE_RETRY_QUEUE, exchange=BRIDGE_RETRY_EXCHANGE, routing_key="#")

    channel.queue_declare(queue=BRIDGE_QUEUE, durable=True, arguments={"x-dead-letter-exchange": BRIDGE_DLX})
    for key in LEGACY_KEYS:
        channel.queue_bind(queue=BRIDGE_QUEUE, exchange=STOREFRONT_EXCHANGE, routing_key=key)
        channel.queue_bind(queue=BRIDGE_QUEUE, exchange=BRIDGE_REQUEUE_EXCHANGE, routing_key=key)

    channel.queue_declare(
        queue=BRIDGE_OUTCOME_QUEUE, durable=True, arguments={"x-dead-letter-exchange": BRIDGE_DLX}
    )
    for key in OUTCOME_KEYS:
        channel.queue_bind(queue=BRIDGE_OUTCOME_QUEUE, exchange=STOREFRONT_EXCHANGE, routing_key=key)
        channel.queue_bind(queue=BRIDGE_OUTCOME_QUEUE, exchange=BRIDGE_REQUEUE_EXCHANGE, routing_key=key)

    # dropped last: a broker already routing outcomes into the legacy queue by catch-all would otherwise deliver every one of them twice
    channel.queue_unbind(queue=BRIDGE_QUEUE, exchange=STOREFRONT_EXCHANGE, routing_key="order.*")
    channel.queue_unbind(queue=BRIDGE_QUEUE, exchange=BRIDGE_REQUEUE_EXCHANGE, routing_key="#")


def _map_data(event_type: str, legacy: dict) -> dict:
    if event_type in (EVENT_ORDER_TRANSITION_SUCCEEDED, EVENT_ORDER_TRANSITION_REJECTED):
        # storefront already writes these in contract shape
        return legacy
    if event_type == EVENT_ORDER_CREATED:
        items = []
        for item in legacy.get("items", []):
            quantity = int(item.get("quantity", 0))
            unit_price = str(item.get("unit_price", "0"))
            line_total = item.get("line_total")
            if line_total is None:
                line_total = Decimal(unit_price) * quantity
            items.append(
                {
                    "source_product_id": _safe_int(item.get("product_id")),
                    "product_code": item.get("product_code", ""),
                    "name": item.get("name", ""),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "line_total": str(line_total),
                }
            )
        return {
            "order_id": legacy["order_id"],
            "customer_id": legacy.get("user_id", 0),
            "status": legacy.get("status", "created"),
            "total": str(legacy.get("total", "0")),
            "recipient_name": legacy.get("recipient_name", ""),
            "phone": legacy.get("phone", ""),
            "address": legacy.get("address", ""),
            "address_verified": legacy.get("address_verified", False),
            "payment_method": legacy.get("payment_method"),
            "items": items,
        }
    if event_type == EVENT_STOCK_CHANGED:
        return {
            "product_code": legacy["product_code"],
            "name": legacy.get("name", ""),
            "stock_quantity": _safe_int(legacy.get("stock_quantity")),
        }
    if event_type == EVENT_CUSTOMER_CHANGED:
        return {
            "customer_id": _safe_int(legacy["customer_id"]),
            "name": legacy.get("name", ""),
            "phone": legacy.get("phone", ""),
        }
    if event_type == EVENT_AUTHZ_CHANGED:
        data = {
            "subject_id": _safe_int(legacy["subject_id"]),
            "authz_version": _safe_int(legacy["authz_version"]),
            "role_active": bool(legacy.get("role_active")),
            "user_active": bool(legacy.get("user_active")),
        }
        # absent and empty are different answers: one means the sender knows of no role, the other that it predates roles entirely. the contract validates the value
        if "roles" in legacy:
            data["roles"] = legacy["roles"]
        return data
    if event_type == EVENT_SNAPSHOT_CONTROL:
        return {
            "run_id": legacy["run_id"],
            "phase": legacy.get("phase", ""),
            "as_of": legacy.get("as_of"),
            "counts": legacy.get("counts", {}),
        }
    return {
        "order_id": legacy["order_id"],
        "status": legacy.get("status", ""),
        "from_status": legacy.get("from_status", ""),
    }


ORIGIN_PRODUCER = "storefront"
RELAYED_BY = "storefront-bridge"

# producer is the semantic owner of the event, not the transport; identity events keep
# producer=identity so downstream provenance/authorization checks stay meaningful
_PRODUCER = {
    EVENT_AUTHZ_CHANGED: "identity",
    EVENT_CUSTOMER_CHANGED: "identity",
}


def is_envelope(payload) -> bool:
    """a body already shaped as a contract envelope, which needs no reconstruction"""
    return isinstance(payload, dict) and "event_type" in payload and "data" in payload


def build_envelope(properties, event_type: str, legacy: dict) -> dict:
    headers = properties.headers or {}
    agg_type, id_key = _AGGREGATE[event_type]
    return {
        "event_id": headers.get("event_id") or properties.message_id,
        "event_type": event_type,
        "schema_version": 1,
        # origin time only; the bridge never invents occurred_at (see _on_message guard)
        "occurred_at": headers["occurred_at"],
        "producer": _PRODUCER.get(event_type, ORIGIN_PRODUCER),
        "relayed_by": RELAYED_BY,
        "snapshot": bool(headers.get("snapshot")),
        "snapshot_run_id": headers.get("snapshot_run_id"),
        "aggregate": {
            "type": agg_type,
            "id": str(legacy[id_key]),
            "version": _safe_int(headers.get("aggregate_version"), 1),
        },
        "correlation_id": headers.get("correlation_id") or properties.correlation_id,
        "causation_id": headers.get("causation_id"),
        "trace_id": None,
        "data": _map_data(event_type, legacy),
    }


class Command(BaseCommand):
    help = "Bridge legacy storefront order events to versioned operations.events (temporary adapter)."

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._publish_conn = None
        self.publish_channel = None

    def handle(self, *args, **options) -> None:
        consume_conn = pika.BlockingConnection(pika.URLParameters(settings.BRIDGE_CONSUME_URL))
        channel = consume_conn.channel()
        declare_bridge_topology(channel)
        # the retry copy is published on this channel and the original acked right after it, so only a confirmed copy may release the original
        channel.confirm_delivery()
        self._open_publish()
        channel.basic_qos(prefetch_count=20)
        for queue in CONSUMED_QUEUES:
            channel.basic_consume(queue=queue, on_message_callback=self._on_message)
        self.stdout.write(
            self.style.SUCCESS(f"storefront bridge: {', '.join(CONSUMED_QUEUES)} -> {messaging.EXCHANGE}")
        )
        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            channel.stop_consuming()
        finally:
            consume_conn.close()
            self._drop_publish()

    def _open_publish(self):
        """
        publish side of the bridge, rebuilt on demand

        it only ever publishes, so nothing services its heartbeats while events are quiet and the
        broker eventually drops it. reusing that dead connection would dead-letter every later
        event, so a failed publish discards it and the next message reconnects
        """
        if self.publish_channel is not None and self.publish_channel.is_open:
            return self.publish_channel
        if self._publish_conn is None or not self._publish_conn.is_open:
            self._publish_conn = pika.BlockingConnection(
                pika.URLParameters(settings.BRIDGE_PUBLISH_URL)
            )
        self.publish_channel = self._publish_conn.channel()
        self.publish_channel.exchange_declare(
            exchange=messaging.EXCHANGE, exchange_type="topic", durable=True
        )
        self.publish_channel.confirm_delivery()
        return self.publish_channel

    def _drop_publish(self) -> None:
        self.publish_channel = None
        if self._publish_conn is not None:
            try:
                self._publish_conn.close()
            except Exception:
                pass
        self._publish_conn = None

    def _on_message(self, channel, method, properties, body) -> None:
        headers = properties.headers or {}
        with log_context(
            message_id=properties.message_id,
            event_id=headers.get("event_id"),
            correlation_id=headers.get("correlation_id") or properties.correlation_id,
            causation_id=headers.get("causation_id"),
        ):
            self._relay(channel, method, properties, body)

    def _relay(self, channel, method, properties, body) -> None:
        event_type = LEGACY_TO_VERSIONED.get(properties.type)
        if event_type is None:
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        try:
            payload = json.loads(body)
            if is_envelope(payload):
                if payload.get("event_type") != event_type:
                    raise ValueError("envelope event_type does not match the delivery type")
                envelope = payload
            else:
                if not (properties.headers or {}).get("occurred_at"):
                    # origin time is required; substituting now() would falsify provenance
                    log.warning("bridge missing_occurred_at -> DLQ",
                                extra={"message_id": properties.message_id})
                    channel.basic_nack(method.delivery_tag, requeue=False)
                    return
                envelope = build_envelope(properties, event_type, payload)
            parse_event(envelope)
        except Exception as exc:
            # the rejected value is the event body, which carries customer data
            log.warning("bridge poison message -> DLQ", extra={"message_id": properties.message_id, "routing_key": method.routing_key, "error_shape": describe_error(exc)})
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        try:
            # ack the legacy delivery only after the versioned publish is confirmed
            messaging.publish_envelope(
                self._open_publish(), messaging.EXCHANGE, event_type, envelope,
                {"event_id": envelope["event_id"], "x-relayed-by": RELAYED_BY},
            )
        except Exception:
            log.exception("bridge publish failed", extra={"event_type": event_type})
            self._drop_publish()
            self._retry(channel, method, properties, body)
            return
        channel.basic_ack(method.delivery_tag)

    def _retry(self, channel, method, properties, body) -> None:
        retries = _safe_int((properties.headers or {}).get(RETRY_HEADER), 0)
        if retries >= BRIDGE_MAX_RETRIES:
            log.warning("bridge retries exhausted -> DLQ")
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        headers = {**(properties.headers or {}), RETRY_HEADER: retries + 1}
        try:
            channel.basic_publish(
                exchange=BRIDGE_RETRY_EXCHANGE,
                routing_key=method.routing_key,
                body=body,
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
        except UnroutableError:
            # no retry queue behind the exchange is a topology fault that no redelivery fixes
            log.warning("bridge retry unroutable -> DLQ")
            channel.basic_nack(method.delivery_tag, requeue=False)
            return
        except Exception:
            log.exception("bridge retry publish failed; requeueing original")
            channel.basic_nack(method.delivery_tag, requeue=True)
            return
        channel.basic_ack(method.delivery_tag)

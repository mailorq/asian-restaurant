import uuid

from django.conf import settings
from django.db import models


class OrderOutbox(models.Model):
    """Transactional outbox for lossless order delivery.

    An order and its outbox row are written in the same DB transaction; the
    relay (publish_outbox command) publishes pending rows to RabbitMQ with
    publisher confirms and marks them published. If the broker is unavailable,
    the row stays pending and is retried — the event is never lost.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        PUBLISHED = "published", "Отправлено"
        # only an operator sets this; the relay retries a failed row forever
        FAILED = "failed", "Снято оператором"

    # ids repeat across aggregates (order 5, customer 5), so an aggregate is the pair of type and id
    class AggregateType(models.TextChoices):
        ORDER = "order"
        PRODUCT = "product"
        CUSTOMER = "customer"
        AUTHZ = "authz"
        SNAPSHOT = "snapshot"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    aggregate_type = models.CharField(max_length=16, choices=AggregateType.choices)
    aggregate_id = models.CharField(max_length=64, db_index=True)
    aggregate_version = models.PositiveIntegerField(default=1)
    event_type = models.CharField(max_length=64, default="order.created")
    routing_key = models.CharField(max_length=64, default="order.created")
    schema_version = models.PositiveSmallIntegerField(default=1)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)
    causation_id = models.UUIDField(null=True, blank=True)
    snapshot = models.BooleanField(default=False)
    snapshot_run_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=64, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            # the relay asks for an earlier pending event of the same aggregate on every claim
            models.Index(
                fields=["aggregate_type", "aggregate_id", "id"],
                condition=models.Q(status="pending"),
                name="outbox_pending_aggregate_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    aggregate_type__in=["order", "product", "customer", "authz", "snapshot"]
                ),
                name="outbox_aggregate_type_known",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_type}:{self.aggregate_type}:{self.aggregate_id} ({self.status})"


class DeliveryAddress(models.Model):
    """A user's delivery address, verified server-side via the geocoder."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses"
    )
    address = models.CharField(max_length=500)
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    provider = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.address


class GeocodeCache(models.Model):
    """Cache of geocoder lookups, keyed by a hash of the normalized query, so
    repeated address checks never re-hit the external provider."""

    query_hash = models.CharField(max_length=64, unique=True)
    query = models.CharField(max_length=500)
    found = models.BooleanField(default=False)
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    display_name = models.CharField(max_length=500, blank=True)
    provider = models.CharField(max_length=32, default="nominatim")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.query


# statuses that count as an open/active order (used for per-customer counters)
ACTIVE_ORDER_STATUSES = ["created", "confirmed", "preparing", "delivering"]


# Order status state machine: allowed forward transitions. Terminal states map
# to an empty set. Enforced in the service layer, not by the DB.
ORDER_TRANSITIONS: dict[str, set[str]] = {
    "created": {"confirmed", "cancelled"},
    "confirmed": {"preparing", "cancelled"},
    "preparing": {"delivering", "cancelled"},
    "delivering": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}


class Order(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Создан"
        CONFIRMED = "confirmed", "Подтверждён"
        PREPARING = "preparing", "Готовится"
        DELIVERING = "delivering", "В доставке"
        DELIVERED = "delivered", "Доставлен"
        CANCELLED = "cancelled", "Отменён"

    class Payment(models.TextChoices):
        CASH = "cash", "Наличными"
        CARD = "card", "Картой"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders"
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CREATED, db_index=True
    )
    payment_method = models.CharField(max_length=8, choices=Payment.choices, default=Payment.CASH)

    # contact snapshot — taken from request.user, never from the checkout form
    phone = models.CharField(max_length=16)
    contact_name = models.CharField(max_length=150, blank=True)
    delivery_address = models.ForeignKey(
        DeliveryAddress, on_delete=models.PROTECT, related_name="orders"
    )

    total = models.DecimalField(max_digits=10, decimal_places=2)

    # idempotency: client key (scoped to the owner) + the (cart, version) it was built from
    idempotency_key = models.CharField(max_length=64)
    source_cart_id = models.CharField(max_length=64)
    source_cart_version = models.PositiveIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_cart_id", "source_cart_version"],
                name="uniq_order_source_cart_version",
            ),
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                name="uniq_order_user_idempotency",
            ),
        ]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"Order #{self.pk} ({self.status})"

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ORDER_TRANSITIONS.get(self.status, set())


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("menu.Product", on_delete=models.PROTECT, related_name="+")
    # snapshot of name/price at checkout so history is stable
    product_name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField()
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="orderitem_qty_gt_0"),
        ]

    def __str__(self) -> str:
        return f"{self.product_name} × {self.quantity}"


class OrderStatusHistory(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="history")
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.order_id}: {self.from_status or '∅'} → {self.to_status}"


class CommandInbox(models.Model):
    """applied operations command with the outcome it produced

    a redelivered command replays this outcome instead of acting again
    """

    command_id = models.UUIDField(unique=True)
    request_event_id = models.UUIDField()
    correlation_id = models.UUIDField()
    outcome_event_id = models.UUIDField(unique=True)
    outcome_type = models.CharField(max_length=64)
    outcome_data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.command_id} -> {self.outcome_type}"


class ProcessedEvent(models.Model):
    """Inbox dedup: records an (event, consumer) pair the operations runtime has
    already handled, so redelivered messages are idempotent."""

    event_id = models.CharField(max_length=64)
    consumer = models.CharField(max_length=64, default="ops")
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event_id", "consumer"], name="uniq_processed_event"),
        ]

    def __str__(self) -> str:
        return f"{self.consumer}:{self.event_id}"

from django.db import transaction

from menu.models import Product, StockAdjustment
from orders.models import OrderOutbox

STOCK_EVENT = "inventory.stock_changed"


def _emit(product: Product, *, snapshot: bool = False, run_id: str = "") -> None:
    OrderOutbox.objects.create(
        aggregate_type=OrderOutbox.AggregateType.PRODUCT,
        aggregate_id=product.code,
        aggregate_version=product.version,
        event_type=STOCK_EVENT,
        routing_key=STOCK_EVENT,
        snapshot=snapshot,
        snapshot_run_id=run_id,
        payload={"product_code": product.code, "name": product.name, "stock_quantity": product.stock_quantity},
    )


def record_stock_change(product: Product, new_quantity: int, *, reason: str, staff=None) -> None:
    # single writer of stock: caller must already hold the Product row lock so the
    # stock write, version bump and outbox event commit atomically in one transaction
    old_quantity = product.stock_quantity
    product.stock_quantity = new_quantity
    product.version += 1
    product.save(update_fields=["stock_quantity", "version"])
    StockAdjustment.objects.create(
        product=product, staff=staff, old_quantity=old_quantity, new_quantity=new_quantity, reason=reason
    )
    _emit(product)


# fields operations projects: a change to any of them bumps the version once and emits one event
PROJECTED_FIELDS = frozenset({"name", "stock_quantity"})


class StaleProduct(Exception):
    def __init__(self, product: Product) -> None:
        self.product = product
        super().__init__(f"product {product.pk} is at version {product.version}")


@transaction.atomic
def update_product(
    product_id: int, changes: dict, *, reason: str, staff=None, expected_version: int | None = None
) -> Product | None:
    # the one writer of an existing product row: it locks the row before reading it, writes only the fields it was given, and refuses a caller whose view of name or stock is older than the row
    product = Product.objects.select_for_update().filter(id=product_id).first()
    if product is None:
        return None
    if expected_version is not None and product.version != expected_version:
        raise StaleProduct(product)
    old_quantity = product.stock_quantity
    written = [
        f
        for f, value in changes.items()
        if f not in PROJECTED_FIELDS or getattr(product, f) != value
    ]
    if not written:
        return product
    for field in written:
        setattr(product, field, changes[field])
    projected = PROJECTED_FIELDS.intersection(written)
    if projected:
        product.version += 1
        written.append("version")
    product.save(update_fields=written)
    if "stock_quantity" in projected:
        StockAdjustment.objects.create(
            product=product, staff=staff, old_quantity=old_quantity, new_quantity=product.stock_quantity, reason=reason
        )
    if projected:
        _emit(product)
    return product


def set_stock(
    product_id: int,
    new_quantity: int,
    *,
    reason: str,
    staff=None,
    expected_version: int | None = None,
) -> Product | None:
    return update_product(
        product_id,
        {"stock_quantity": new_quantity},
        reason=reason,
        staff=staff,
        expected_version=expected_version,
    )


def emit_state(product: Product, *, snapshot: bool = False, run_id: str = "") -> None:
    # re-emit current state at the current version: live seeds a projection, snapshot
    # records a reconciliation expectation
    _emit(product, snapshot=snapshot, run_id=run_id)

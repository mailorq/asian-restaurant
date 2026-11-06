import datetime as dt
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from menu.models import Product, ProductChange, StockAdjustment
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
        payload={
            "product_code": product.code,
            "name": product.name,
            "stock_quantity": product.stock_quantity,
        },
    )


def record_stock_change(
    product: Product, new_quantity: int, *, reason: str, source: str, staff=None
) -> None:
    # single writer of stock: caller must already hold the Product row lock so the
    # stock write, version bump and outbox event commit atomically in one transaction
    old_quantity = product.stock_quantity
    product.stock_quantity = new_quantity
    product.version += 1
    product.save(update_fields=["stock_quantity", "version"])
    StockAdjustment.objects.create(
        product=product,
        staff=staff,
        source=source,
        old_quantity=old_quantity,
        new_quantity=new_quantity,
        reason=reason,
    )
    _emit(product)


# fields operations projects: a change to any of them bumps the version once and emits one event
PROJECTED_FIELDS = frozenset({"name", "stock_quantity"})
# operations knows a product by its code, and the version belongs to this writer
FIXED_FIELDS = frozenset({"code", "version"})
_NOT_SIGNED = {
    StockAdjustment.Source.SALE: "продажа",
    StockAdjustment.Source.CANCELLATION: "отмена заказа",
    StockAdjustment.Source.SYSTEM: "система",
}


class StaleProduct(Exception):
    def __init__(self, product: Product) -> None:
        self.product = product
        super().__init__(f"product {product.pk} is at version {product.version}")


@dataclass(frozen=True)
class FieldConflict:
    field: str
    # what the row holds now, who left it there and when; author is None when nobody knows
    current: object
    author: str | None
    at: dt.datetime | None


class ProductConflict(Exception):
    """fields the caller writes changed after it was shown them"""

    def __init__(self, product: Product, conflicts: list[FieldConflict]) -> None:
        self.product = product
        self.conflicts = conflicts
        super().__init__(f"product {product.pk}: {', '.join(c.field for c in conflicts)} changed")


def stored(field: str, value):
    """a value as the row compares it, whether it comes from the row, a form or a page that showed it"""
    if field == "image":
        return getattr(value, "name", value) or ""
    return Product._meta.get_field(field).to_python(value)


def shown(field: str, value) -> str:
    """the text a page carries for a value it showed; stored() reads it back"""
    value = stored(field, value)
    return "" if value is None else str(value)


def _same(field: str, current, seen) -> bool:
    try:
        return stored(field, current) == stored(field, seen)
    except ValidationError:
        return False


def author_of(product: Product, field: str) -> tuple[str | None, dt.datetime | None]:
    """who left the field as it is: a person only for an edit a person made"""
    if field == "stock_quantity":
        row = (
            product.stock_adjustments.select_related("staff").order_by("-created_at", "-id").first()
        )
        if row is None:
            return None, None
        if row.moved_by() in _NOT_SIGNED:
            return _NOT_SIGNED[row.moved_by()], row.created_at
        return (row.staff.username if row.staff_id else None), row.created_at
    row = (
        ProductChange.objects.filter(product=product, field=field)
        .select_related("staff")
        .order_by("-created_at", "-id")
        .first()
    )
    if row is None:
        return None, None
    return (
        row.staff.username if row.staff_id else _NOT_SIGNED[StockAdjustment.Source.SYSTEM]
    ), row.created_at


def conflicts(product: Product, seen: dict) -> list[FieldConflict]:
    """the fields whose value in the row is not the one the caller saw; the caller holds the row lock"""
    found = []
    for field, value in seen.items():
        current = getattr(product, field)
        if not _same(field, current, value):
            author, at = author_of(product, field)
            found.append(FieldConflict(field, current, author, at))
    return found


@transaction.atomic
def update_product(
    product_id: int,
    changes: dict,
    *,
    reason: str,
    staff=None,
    expected_version: int | None = None,
    seen: dict | None = None,
) -> Product | None:
    """
    the one writer of an existing product row: it locks the row before reading it and writes only the fields it was
    given. it refuses a caller whose view of name or stock is older than the row (expected_version), and one that
    writes a field which no longer holds the value it was shown (seen, by field)
    """
    fixed = FIXED_FIELDS.intersection(changes)
    if fixed:
        raise ValueError(f"{', '.join(sorted(fixed))} of an existing product cannot change")
    product = Product.objects.select_for_update().filter(id=product_id).first()
    if product is None:
        return None
    if expected_version is not None and product.version != expected_version:
        raise StaleProduct(product)
    clashes = conflicts(product, {f: seen[f] for f in changes if seen and f in seen})
    if clashes:
        raise ProductConflict(product, clashes)
    before = {f: getattr(product, f) for f in changes}
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
    product.save(update_fields=[*written, "version"] if projected else written)
    if "stock_quantity" in projected:
        StockAdjustment.objects.create(
            product=product,
            staff=staff,
            source=StockAdjustment.Source.STAFF if staff else StockAdjustment.Source.SYSTEM,
            old_quantity=before["stock_quantity"],
            new_quantity=product.stock_quantity,
            reason=reason,
        )
    ProductChange.objects.bulk_create(
        ProductChange(
            product=product,
            field=field,
            old_value=shown(field, before[field]),
            new_value=shown(field, getattr(product, field)),
            staff=staff,
            reason=reason,
        )
        for field in written
        if field != "stock_quantity" and not _same(field, before[field], getattr(product, field))
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

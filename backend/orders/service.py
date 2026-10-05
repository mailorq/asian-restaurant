import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Max
from ninja.errors import HttpError

from cart import service as cart_service
from menu import inventory
from menu.models import Product
from orders import geocode as geo
from orders.models import (
    DeliveryAddress,
    Order,
    OrderItem,
    OrderOutbox,
    OrderStatusHistory,
)

log = logging.getLogger(__name__)


class CheckoutError(Exception):
    def __init__(self, code: str, message: str, items: list | None = None) -> None:
        self.code = code
        self.message = message
        self.items = items or []
        super().__init__(message)


def _order_payload(order: Order, items: list[OrderItem]) -> dict:
    return {
        "order_id": order.id,
        "user_id": order.user_id,
        "status": order.status,
        "total": str(order.total),
        "phone": order.phone,
        "recipient_name": order.contact_name,
        "address": order.delivery_address.address,
        "address_verified": order.delivery_address.is_verified,
        "payment_method": order.payment_method,
        "items": [
            {
                "product_id": i.product_id,
                "product_code": i.product.code,
                "name": i.product_name,
                "quantity": i.quantity,
                "unit_price": str(i.unit_price),
                "line_total": str(i.line_total),
            }
            for i in items
        ],
        "created_at": order.created_at.isoformat(),
    }


def _geocode_safe(raw_address: str) -> geo.GeocodeResult:
    try:
        return geo.geocode(raw_address)
    except geo.GeocoderUnavailable:
        return geo.GeocodeResult(found=False)


def _source_cart_id(user, cart_id: str) -> str:
    return f"u:{user.id}:{cart_id}"


def _details(user, raw_address: str, payment_method: str, recipient_name: str) -> tuple[str, str, str]:
    # what an order keeps of its form, in the form it keeps it
    return raw_address.strip()[:500], payment_method, (recipient_name or "").strip() or user.first_name or ""


@transaction.atomic
def _create_order(
    user,
    cart_items,
    source_cart_id,
    cart_version,
    details,
    geo_result,
    idempotency_key,
    read_after,
):
    # the customer's checkouts commit one at a time from here, and a snapshot read before another of
    # their orders committed may hold what that order bought: it is turned back, not bought twice.
    # orders are created nowhere else, so one committed after the snapshot has a larger key
    get_user_model().objects.select_for_update(no_key=True).filter(pk=user.pk).first()
    later = Order.objects.filter(user=user, pk__gt=read_after).exclude(
        source_cart_id=source_cart_id, source_cart_version=cart_version
    )
    if later.exists():
        raise CheckoutError("cart_changed", "Корзина изменилась, пока оформлялся заказ - проверьте ее состав")

    locked = {
        p.id: p
        for p in Product.objects.select_for_update()
        .filter(id__in=list(cart_items.keys()))
        .order_by("id")
    }

    problems = []
    for product_id, quantity in cart_items.items():
        product = locked.get(product_id)
        if product is None or not product.is_active:
            problems.append({"product_id": product_id, "name": product.name if product else "", "reason": "unavailable"})
        elif product.stock_quantity < quantity:
            problems.append(
                {"product_id": product_id, "name": product.name, "reason": "insufficient_stock", "available": product.stock_quantity}
            )
    if problems:
        raise CheckoutError("cart_changed", "Корзина изменилась — проверьте наличие товаров", problems)

    address_text, payment_method, contact_name = details
    address = DeliveryAddress.objects.create(
        user=user,
        address=address_text,
        lat=geo_result.lat,
        lng=geo_result.lng,
        is_verified=geo_result.found,
        provider="nominatim" if geo_result.found else "",
    )

    order = Order.objects.create(
        user=user,
        status=Order.Status.CREATED,
        payment_method=payment_method,
        phone=user.phone or "",  # from request.user
        contact_name=contact_name,
        delivery_address=address,
        total=Decimal("0.00"),
        idempotency_key=idempotency_key,
        source_cart_id=source_cart_id,
        source_cart_version=cart_version,
        cart_clear_pending=True,
    )

    total = Decimal("0.00")
    items: list[OrderItem] = []
    for product_id, quantity in cart_items.items():
        product = locked[product_id]
        unit_price = product.price  # server price is authoritative at checkout
        line_total = unit_price * quantity
        total += line_total
        items.append(
            OrderItem(
                order=order,
                product=product,
                product_name=product.name,
                unit_price=unit_price,
                quantity=quantity,
                line_total=line_total,
            )
        )
        inventory.record_stock_change(
            product, product.stock_quantity - quantity, reason=f"order #{order.id}"
        )

    OrderItem.objects.bulk_create(items)
    order.total = total
    order.save(update_fields=["total"])

    OrderStatusHistory.objects.create(
        order=order, from_status="", to_status=Order.Status.CREATED, changed_by=user, note="checkout"
    )
    OrderOutbox.objects.create(
        aggregate_type=OrderOutbox.AggregateType.ORDER,
        aggregate_id=str(order.id),
        aggregate_version=1,
        event_type="order.created",
        routing_key="order.created",
        payload=_order_payload(order, items),
    )
    return order


def _bought(order: Order) -> dict[int, int]:
    return {item.product_id: item.quantity for item in order.items.all()}


def _unknown_orders(user, cart):
    # orders of a previous release recorded nothing about the cart they were built from. a cart of
    # that generation which moved on since may or may not still hold what they bought: a write after
    # a failed clearing and a write after a successful one look the same, so only its owner can tell
    return Order.objects.filter(
        source_cart_id=_source_cart_id(user, cart.cart_id), cart_clear_pending__isnull=True
    )


async def purchases_to_settle(user) -> bool:
    if await Order.objects.filter(user=user, cart_clear_pending=True).aexists():
        return True
    cart = await cart_service.read_state(cart_service.user_key(user.id))
    return await _unknown_orders(user, cart).aexists()


async def cart_review(user) -> int | None:
    """the order of a previous release the cart may still hold, until its owner confirms the cart"""
    cart = await cart_service.read_state(cart_service.user_key(user.id))
    return await _unknown_orders(user, cart).order_by("pk").values_list("pk", flat=True).afirst()


def confirm_cart(user, expected_version: int) -> bool:
    """the owner keeps the cart as it is at the version they saw; false when it has moved on since"""
    key = cart_service.user_key(user.id)
    cart = cart_service.read_sync(key)
    if cart.version != expected_version:
        return False
    unknown = _unknown_orders(user, cart)
    if not unknown.exists():
        # nothing of a previous release to settle: the cart is already as its owner wants it
        return True
    # claim the seen snapshot before settling: the version check and its effect are one atomic step,
    # so a cart write at this version and this confirmation cannot both win and a change that commits
    # after the owner looked cannot be approved under the version they saw. a crash before the settle
    # below leaves the orders unsettled and the cart at a version the owner re-confirms
    if not cart_service.claim_sync(key, expected_version, cart.cart_id):
        return False
    unknown.update(cart_clear_pending=False)
    return True


def settle_purchases(user) -> None:
    # an order stays pending from its commit until what it bought is gone from the cart it was
    # built from. the cart store records every order it subtracted, so a retry, a crash in between
    # or two requests at once subtract it once, and whatever the cart gained since stays
    key = cart_service.user_key(user.id)
    pending = Order.objects.filter(user=user, cart_clear_pending=True).prefetch_related("items")
    for order in pending:
        cart_id = order.source_cart_id.removeprefix(f"u:{user.id}:")
        cart_service.remove_purchased_sync(key, cart_id, order.pk, _bought(order))
        Order.objects.filter(pk=order.pk).update(cart_clear_pending=False)

    cart = cart_service.read_sync(key)
    unknown = list(_unknown_orders(user, cart).prefetch_related("items"))
    if not unknown:
        return
    # a cart still at the version and contents of such an order is cleared, as the previous release
    # itself did on a retry
    untouched = [o for o in unknown if o.source_cart_version == cart.version and _bought(o) == cart.items]
    if untouched and not cart_service.clear_sync(key, cart.version, cart.cart_id):
        return
    # an empty cart has nothing of theirs left to buy, and whatever it gains from here on is new;
    # a cart that moved on and still holds lines waits for its owner, see cart_review
    if untouched or not cart.items:
        Order.objects.filter(pk__in=[o.pk for o in unknown]).update(cart_clear_pending=False)


def _after_commit(user, order: Order) -> Order:
    # the order is committed, so it is the answer whatever the cart store does now: it stays
    # pending, and the next request to the cart or the next checkout subtracts what it bought
    try:
        settle_purchases(user)
    except HttpError:
        log.warning(
            "cart not cleared after checkout",
            extra={"order_id": order.pk, "user_id": order.user_id},
        )
    return order


def _replayed(user, order: Order, details: tuple[str, str, str]) -> Order:
    # the same form sent again gets its order; other details under it are another intent, and
    # answering it with this order would confirm what was never saved. judged by what the order
    # kept, so an order of a previous release is judged the same way
    if (order.delivery_address.address, order.payment_method, order.contact_name) != details:
        raise CheckoutError(
            "checkout_replayed",
            f"Заказ №{order.pk} уже оформлен с другими данными, проверьте его в разделе «Мои заказы»",
        )
    return _after_commit(user, order)


def checkout(user, raw_address: str, payment_method: str, idempotency_key: str, recipient_name: str = "") -> Order:
    details = _details(user, raw_address, payment_method, recipient_name)
    existing = Order.objects.filter(user=user, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _replayed(user, existing, details)

    # taken before the cart is read: an order committed after it is one this snapshot cannot account for
    read_after = Order.objects.filter(user=user).aggregate(latest=Max("pk"))["latest"] or 0
    key = cart_service.user_key(user.id)
    cart = cart_service.read_sync(key)
    # a cart still at the version of an order is that order's snapshot sent again
    if cart.items:
        existing = Order.objects.filter(
            source_cart_id=_source_cart_id(user, cart.cart_id), source_cart_version=cart.version
        ).first()
        if existing is not None:
            return _replayed(user, existing, details)

    # what earlier orders bought leaves the cart before it is read, or this order would buy it again
    settle_purchases(user)
    cart = cart_service.read_sync(key)
    cart_items, cart_version = cart.items, cart.version
    if not cart_items:
        raise CheckoutError("empty_cart", "Корзина пуста")
    review = _unknown_orders(user, cart).order_by("pk").values_list("pk", flat=True).first()
    if review is not None:
        raise CheckoutError(
            "cart_needs_review",
            f"Корзина могла сохранить позиции заказа №{review}, оформленного до обновления сайта - "
            "проверьте ее состав и подтвердите его",
        )
    source_cart_id = _source_cart_id(user, cart.cart_id)

    geo_result = _geocode_safe(raw_address)  # external http, kept out of the transaction

    try:
        order = _create_order(
            user,
            cart_items,
            source_cart_id,
            cart_version,
            details,
            geo_result,
            idempotency_key,
            read_after,
        )
    except IntegrityError:
        # a concurrent submit won the unique(idempotency_key) / (source_cart_id, version) race
        existing = (
            Order.objects.filter(user=user, idempotency_key=idempotency_key).first()
            or Order.objects.filter(source_cart_id=source_cart_id, source_cart_version=cart_version).first()
        )
        if existing is not None:
            return _replayed(user, existing, details)
        raise

    # remove exactly what was bought: an item added mid-checkout stays, and the purchased
    # items never survive to be sold a second time by the next checkout
    return _after_commit(user, order)


def _restore_stock(order: Order, staff=None) -> None:
    items = list(order.items.all())
    if not items:
        return
    products = {
        p.id: p
        for p in Product.objects.select_for_update()
        .filter(id__in=[item.product_id for item in items])
        .order_by("id")
    }
    for item in items:
        product = products.get(item.product_id)
        if product is None:
            continue
        inventory.record_stock_change(
            product,
            product.stock_quantity + item.quantity,
            reason=f"cancelled order #{order.id}",
            staff=staff,
        )


@transaction.atomic
def transition(order: Order, new_status: str, changed_by=None, note: str = "",
               expected_status: str | None = None, causation_id=None) -> Order:
    locked = Order.objects.select_for_update().get(pk=order.pk)
    if expected_status is not None and locked.status != expected_status:
        raise CheckoutError("stale_order", f"Заказ уже в статусе «{locked.get_status_display()}»")
    if not locked.can_transition_to(new_status):
        raise CheckoutError("invalid_transition", f"Недопустимый переход {locked.status} → {new_status}")

    if new_status == Order.Status.CANCELLED:
        _restore_stock(locked, staff=changed_by)

    previous = locked.status
    locked.status = new_status
    locked.save(update_fields=["status", "updated_at"])
    OrderStatusHistory.objects.create(
        order=locked, from_status=previous, to_status=new_status, changed_by=changed_by, note=note
    )
    OrderOutbox.objects.create(
        aggregate_type=OrderOutbox.AggregateType.ORDER,
        aggregate_id=str(locked.id),
        aggregate_version=OrderStatusHistory.objects.filter(order=locked).count(),
        event_type="order.status_changed",
        routing_key="order.status_changed",
        causation_id=causation_id,
        payload={"order_id": locked.id, "status": new_status, "from_status": previous},
    )
    return locked


def emit_order_state(order: Order, *, snapshot: bool = False, run_id: str = "") -> None:
    # order aggregate version is the length of its status history
    items = list(order.items.select_related("product").all())
    OrderOutbox.objects.create(
        aggregate_type=OrderOutbox.AggregateType.ORDER,
        aggregate_id=str(order.id),
        aggregate_version=OrderStatusHistory.objects.filter(order=order).count(),
        event_type="order.created",
        routing_key="order.created",
        snapshot=snapshot,
        snapshot_run_id=run_id,
        payload=_order_payload(order, items),
    )

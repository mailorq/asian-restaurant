import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from cart import service as cart_service
from orders import service as order_service
from orders.models import Order, OrderOutbox
from orders.service import CheckoutError

pytestmark = pytest.mark.django_db


def _checkout(client, address="ул. Пушкина, 12", payment="cash", key="idem-00000001", recipient=None):
    body = {"address": address, "payment_method": payment, "idempotency_key": key}
    if recipient is not None:
        body["recipient_name"] = recipient
    return client.post("/api/orders/checkout", data=json.dumps(body), content_type="application/json")


def test_checkout_creates_order_and_decrements_stock(client, user, make_product, seed_cart):
    product = make_product(price="200.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    client.force_login(user)

    resp = _checkout(client)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["total"] == 400.0
    assert data["phone"] == user.phone  # from request.user, not the form
    assert data["items"][0]["product_id"] == product.id
    assert data["items"][0]["quantity"] == 2

    product.refresh_from_db()
    assert product.stock_quantity == 8  # 10 - 2
    assert cart_service.read_sync(cart_service.user_key(user.id))[0] == {}  # cart cleared
    assert OrderOutbox.objects.filter(aggregate_id=str(data["id"]), event_type="order.created").exists()


def test_cancel_restores_stock(user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 3}, version=1)
    order = order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-cancel-1")
    product.refresh_from_db()
    assert product.stock_quantity == 7

    order_service.transition(order, "cancelled", changed_by=user, expected_status="created")

    product.refresh_from_db()
    assert product.stock_quantity == 10


def test_cancel_restores_stock_only_once(user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 3}, version=1)
    order = order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-cancel-2")
    order_service.transition(order, "cancelled", changed_by=user)

    with pytest.raises(CheckoutError) as exc:
        order_service.transition(order, "cancelled", changed_by=user)
    assert exc.value.code == "invalid_transition"

    product.refresh_from_db()
    assert product.stock_quantity == 10


def test_checkout_removes_only_purchased_items_when_cart_changed_midway(
    user, make_product, seed_cart, monkeypatch
):
    bought = make_product(price="100.00", stock=10)
    added = make_product(price="50.00", stock=10)
    key = cart_service.user_key(user.id)
    real_read = cart_service.read_sync

    seed_cart(user.id, {bought.id: 2, added.id: 1}, version=2)
    monkeypatch.setattr(cart_service, "read_sync", lambda k: cart_service.CartState({bought.id: 2}, 1, real_read(k).cart_id))

    order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-mid-01")

    items = real_read(key).items
    assert bought.id not in items  # purchased items never survive to be sold again
    assert items == {added.id: 1}  # the concurrent addition is preserved


def test_retry_after_lost_response_clears_the_uncleared_cart(user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    key = cart_service.user_key(user.id)
    order = order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-retry-1")

    seed_cart(user.id, {product.id: 2}, version=order.source_cart_version)

    again = order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-retry-1")

    assert again.pk == order.pk
    assert cart_service.read_sync(key)[0] == {}
    product.refresh_from_db()
    assert product.stock_quantity == 8  # the replay never decrements stock twice


def test_retry_does_not_wipe_a_rebuilt_cart_at_the_same_version(user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    other = make_product(price="70.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    key = cart_service.user_key(user.id)
    order = order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-rebuild-1")

    seed_cart(user.id, {other.id: 1}, version=order.source_cart_version)

    order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-rebuild-1")

    items = cart_service.read_sync(key).items
    assert items == {other.id: 1}


def test_checkout_requires_auth(client, user, make_product, seed_cart):
    product = make_product(stock=5)
    seed_cart(user.id, {product.id: 1})
    resp = _checkout(client)
    assert resp.status_code == 401


def test_checkout_empty_cart_returns_422(client, user):
    client.force_login(user)
    resp = _checkout(client)
    assert resp.status_code == 422
    assert resp.json()["code"] == "empty_cart"


def test_checkout_insufficient_stock_returns_409(client, user, make_product, seed_cart):
    product = make_product(stock=1)
    seed_cart(user.id, {product.id: 5}, version=1)
    client.force_login(user)

    resp = _checkout(client)

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "cart_changed"
    assert body["items"][0]["reason"] == "insufficient_stock"
    product.refresh_from_db()
    assert product.stock_quantity == 1  # unchanged — transaction rolled back


def test_checkout_idempotent_by_key(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    client.force_login(user)

    first = _checkout(client, key="idem-repeat-1")
    # re seed the cart (a retry would still carry the same idempotency key)
    seed_cart(user.id, {product.id: 2}, version=1)
    second = _checkout(client, key="idem-repeat-1")

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert Order.objects.count() == 1
    product.refresh_from_db()
    assert product.stock_quantity == 8  # decremented once


def test_checkout_idempotent_by_cart_version(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 3}, version=7)
    client.force_login(user)

    first = _checkout(client, key="idem-cart-a")
    seed_cart(user.id, {product.id: 3}, version=7)  # same cart version resubmitted
    second = _checkout(client, key="idem-cart-b")

    assert first.json()["id"] == second.json()["id"]  # same order, not a duplicate
    assert Order.objects.count() == 1
    product.refresh_from_db()
    assert product.stock_quantity == 7  # decremented once


def test_order_list_and_detail(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)
    order_id = _checkout(client).json()["id"]

    listing = client.get("/api/orders")
    assert listing.status_code == 200
    assert [o["id"] for o in listing.json()["items"]] == [order_id]

    detail = client.get(f"/api/orders/{order_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == order_id


def test_order_detail_is_scoped_to_owner(client, user, make_product, seed_cart, django_user_model):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)
    order_id = _checkout(client).json()["id"]

    other = django_user_model.objects.create_user(username="+79990000002", password="Pass!2345")
    client.force_login(other)
    assert client.get(f"/api/orders/{order_id}").status_code == 404


def test_checkout_address_unverified_when_geocoder_off(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)

    data = _checkout(client).json()
    assert data["address_verified"] is False  # geocoder disabled in tests
    assert data["address"] == "ул. Пушкина, 12"


def test_checkout_uses_recipient_name_without_touching_profile(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)

    order_id = _checkout(client, key="idem-recip-1", recipient="Пётр").json()["id"]

    order = Order.objects.get(pk=order_id)
    assert order.contact_name == "Пётр"  # per-order recipient
    assert order.phone == user.phone  # phone still from the account
    user.refresh_from_db()
    assert user.first_name == "Иван"  # profile unchanged


def test_checkout_defaults_recipient_to_first_name(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)

    order_id = _checkout(client, key="idem-recip-2").json()["id"]
    assert Order.objects.get(pk=order_id).contact_name == "Иван"


def test_last_address_returns_most_recent(client, user, make_product, seed_cart):
    client.force_login(user)
    assert client.get("/api/orders/address/last").json() == {"address": "", "is_verified": False}

    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    _checkout(client, address="ул. Садовая, 5", key="idem-addr-1")

    data = client.get("/api/orders/address/last").json()
    assert data["address"] == "ул. Садовая, 5"


def test_transition_enforces_state_machine(user, make_product, seed_cart, client):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)
    order = Order.objects.get(pk=_checkout(client).json()["id"])

    updated = order_service.transition(order, "confirmed", changed_by=user)
    assert updated.status == "confirmed"  # transition returns the freshly-locked order
    assert updated.history.filter(to_status="confirmed").exists()

    with pytest.raises(CheckoutError):
        order_service.transition(order, "delivered")  # confirmed -> delivered not allowed


def test_checkout_writes_enriched_outbox(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)
    order_id = _checkout(client, key="idem-outbox-1").json()["id"]

    row = OrderOutbox.objects.get(aggregate_id=str(order_id), event_type="order.created")
    assert row.routing_key == "order.created"
    assert row.aggregate_version == 1
    assert row.schema_version == 1
    assert row.event_id is not None


def test_transition_rejects_stale_expected_status(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    client.force_login(user)
    order = Order.objects.get(pk=_checkout(client).json()["id"])

    order_service.transition(order, "confirmed", changed_by=user, expected_status="created")
    with pytest.raises(CheckoutError) as exc:
        order_service.transition(order, "cancelled", changed_by=user, expected_status="created")
    assert exc.value.code == "stale_order"


def test_idempotency_key_scoped_per_user(client, make_product, seed_cart, django_user_model):
    u1 = django_user_model.objects.create_user(username="+380670000001", password="x", phone="+380670000001")
    u2 = django_user_model.objects.create_user(username="+380670000002", password="x", phone="+380670000002")
    product = make_product(stock=10)

    seed_cart(u1.id, {product.id: 1}, version=1)
    client.force_login(u1)
    order1 = _checkout(client, key="shared-idem-key").json()["id"]

    seed_cart(u2.id, {product.id: 1}, version=1)
    client.force_login(u2)
    order2 = _checkout(client, key="shared-idem-key").json()["id"]

    assert order1 != order2  # same key, different owners -> two distinct orders
    assert Order.objects.filter(idempotency_key="shared-idem-key").count() == 2


def test_live_seeding_refuses_second_run(user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-seed-1")

    # re-emitting at an already-projected version would dead-letter every event
    with pytest.raises(CommandError):
        call_command("emit_source_state")

    call_command("emit_source_state", "--force-live")  # deliberate override still works


def test_live_seeding_allowed_on_empty_outbox():
    call_command("emit_source_state")  # nothing emitted yet: bootstrap is safe

import json
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import F

from cart import service as cart_service
from menu.models import Product
from orders.models import Order

# the last schema of the release before purchases were recorded against the cart
BEFORE = ("orders", "0012_outbox_closed_rows_hold_their_aggregate")
ADDRESS = "ул. Пушкина, 12"

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def migrations():
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    yield leaves
    MigrationExecutor(connection).migrate(leaves)


def _place_as_before(user, product, quantity, cart_id, version, key="old-form", address=ADDRESS):
    # the rows the previous release wrote for an order, through its own schema: an insert names
    # only the columns that release knew
    apps = MigrationExecutor(connection).loader.project_state([BEFORE]).apps
    price = Decimal(product.price)
    place = apps.get_model("orders", "DeliveryAddress").objects.create(user_id=user.pk, address=address)
    order = apps.get_model("orders", "Order").objects.create(
        user_id=user.pk,
        payment_method="cash",
        phone=user.phone,
        contact_name=user.first_name,
        delivery_address=place,
        total=price * quantity,
        idempotency_key=key,
        source_cart_id=f"u:{user.pk}:{cart_id}",
        source_cart_version=version,
    )
    apps.get_model("orders", "OrderItem").objects.create(
        order=order,
        product_id=product.pk,
        product_name=product.name,
        unit_price=price,
        quantity=quantity,
        line_total=price * quantity,
    )
    Product.objects.filter(pk=product.pk).update(stock_quantity=F("stock_quantity") - quantity)
    return order.pk


def _cart(user):
    return cart_service.read_sync(cart_service.user_key(user.id))


def _checkout(api, key, address=ADDRESS):
    body = {"address": address, "payment_method": "cash", "idempotency_key": key}
    return api.post("/api/orders/checkout", data=json.dumps(body))


def _lines(cart):
    return [(line["product_id"], line["quantity"]) for line in cart["items"]]


def test_an_order_placed_before_the_migration_with_its_cart_left_full_is_not_bought_again(
    migrations, api, user, make_product, seed_cart
):
    # two of three bought and the clearing failed: a render would clamp the line to the one left
    product = make_product(price="100.00", stock=3)
    MigrationExecutor(connection).migrate([BEFORE])
    seed_cart(user.id, {product.id: 2}, version=1)
    old = _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    MigrationExecutor(connection).migrate(migrations)
    api.force_login(user)

    cart = api.get("/api/cart").json()
    again = _checkout(api, key="new-form")

    assert Order.objects.get(pk=old).cart_clear_pending is False
    assert cart["items"] == [] and cart["adjustments"] == []
    assert again.status_code == 422 and again.json()["code"] == "empty_cart"
    assert list(Order.objects.values_list("pk", flat=True)) == [old]
    product.refresh_from_db()
    assert product.stock_quantity == 1


def test_an_order_an_old_writer_places_during_the_migration_is_settled_before_the_next_write(
    api, user, make_product, seed_cart
):
    bought = make_product(price="100.00", stock=3)
    added = make_product(price="50.00", stock=10)
    seed_cart(user.id, {bought.id: 2}, version=1)
    old = _place_as_before(user, bought, 2, _cart(user).cart_id, 1)
    assert Order.objects.get(pk=old).cart_clear_pending is None
    api.force_login(user)

    write = api.post("/api/cart/items", data=json.dumps({"product_id": added.id}))
    second = _checkout(api, key="new-form")

    assert _lines(write.json()) == [(added.id, 1)]
    assert second.status_code == 200
    assert [(item["product_id"], item["quantity"]) for item in second.json()["items"]] == [(added.id, 1)]
    bought.refresh_from_db()
    assert bought.stock_quantity == 1


def test_a_cart_the_previous_release_did_clear_is_not_cleared_again(api, user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    old = _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    # that release removed what it sold, then one more of the same went into the cart
    seed_cart(user.id, {product.id: 1}, version=3)
    api.force_login(user)

    cart = api.get("/api/cart").json()

    assert _lines(cart) == [(product.id, 1)]
    assert Order.objects.get(pk=old).cart_clear_pending is None


def test_a_snapshot_of_an_order_the_previous_release_placed_is_judged_by_what_it_kept(
    api, user, make_product, seed_cart
):
    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    old = _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    api.force_login(user)

    edited = _checkout(api, key="new-form", address="ул. Лермонтова, 7")
    same = _checkout(api, key="newer-form")

    assert edited.status_code == 409 and edited.json()["code"] == "checkout_replayed"
    assert f"№{old}" in edited.json()["message"]
    assert same.status_code == 200 and same.json()["id"] == old
    assert list(Order.objects.values_list("pk", flat=True)) == [old]
    assert _cart(user).items == {}
    assert Product.objects.get(pk=product.pk).stock_quantity == 8


def _as_before(write, *args):
    # a cart write of the previous release: the same cart service, reached without settling first
    cart_service._clients.clear()
    return async_to_sync(write)(*args)


def _old_clearing(user, product):
    # what the previous release did when its clearing worked: the line went and the version moved on
    key = cart_service.user_key(user.id)
    cart_service._sync_redis().hdel(key, str(product.id))
    cart_service._sync_redis().hincrby(key, cart_service.VERSION_FIELD, 1)


def _review(api):
    return api.get("/api/cart").json()["review_order"]


def _confirm(api):
    version = api.get("/api/cart").json()["version"]
    return api.post("/api/cart/review", data=json.dumps({"expected_version": version}))


def _orders():
    return list(Order.objects.order_by("pk").values_list("pk", flat=True))


def test_a_cart_the_previous_release_rendered_after_a_failed_clearing_waits_for_its_owner(
    api, user, make_product, seed_cart
):
    # three in stock, two bought and not cleared, then the old render clamped the line to the one left
    product = make_product(price="100.00", stock=3)
    key = seed_cart(user.id, {product.id: 2}, version=1)
    old = _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    _as_before(cart_service.render, key, {product.id: 2}, 1)
    api.force_login(user)

    cart = api.get("/api/cart").json()
    refused = _checkout(api, key="after-upgrade")
    confirmed = _confirm(api)
    placed = _checkout(api, key="after-review")

    assert _lines(cart) == [(product.id, 1)] and cart["review_order"] == old
    assert refused.status_code == 409 and refused.json()["code"] == "cart_needs_review"
    assert f"№{old}" in refused.json()["message"]
    assert confirmed.status_code == 200 and confirmed.json()["review_order"] is None
    # what the owner confirmed is what is bought, once
    assert placed.status_code == 200
    assert [(i["product_id"], i["quantity"]) for i in placed.json()["items"]] == [(product.id, 1)]
    assert _orders() == [old, placed.json()["id"]]


@pytest.mark.parametrize("write", ["add", "set", "remove"])
def test_any_write_of_the_previous_release_leaves_the_cart_to_its_owner(
    api, user, make_product, seed_cart, write
):
    bought = make_product(price="100.00", stock=10)
    other = make_product(price="50.00", stock=10)
    key = seed_cart(user.id, {bought.id: 2, other.id: 1}, version=1)
    old = _place_as_before(user, bought, 2, _cart(user).cart_id, 1)
    if write == "add":
        _as_before(cart_service.add, key, other.id, 1, None)
    elif write == "set":
        _as_before(cart_service.set_qty, key, bought.id, 3, None)
    else:
        _as_before(cart_service.remove, key, other.id, None)
    api.force_login(user)

    review = _review(api)
    refused = _checkout(api, key="after-upgrade")

    assert review == old
    assert refused.status_code == 409 and refused.json()["code"] == "cart_needs_review"
    assert _orders() == [old]


def test_a_line_added_again_after_a_clearing_that_worked_is_not_taken_away(api, user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    key = seed_cart(user.id, {product.id: 2}, version=1)
    old = _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    _old_clearing(user, product)
    _as_before(cart_service.add, key, product.id, 1, None)
    api.force_login(user)

    review = _review(api)
    _confirm(api)
    placed = _checkout(api, key="after-review")

    # the clearing that worked looks like the one that failed, so the owner decides, and nothing is subtracted
    assert review == old
    assert [(i["product_id"], i["quantity"]) for i in placed.json()["items"]] == [(product.id, 1)]


def test_a_cart_of_a_later_generation_is_no_order_s_business(api, user, make_product, seed_cart):
    product = make_product(price="100.00", stock=10)
    key = seed_cart(user.id, {product.id: 2}, version=1)
    _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    # the cart expired and a new one began
    cart_service._sync_redis().delete(key)
    api.force_login(user)
    api.post("/api/cart/items", data=json.dumps({"product_id": product.id}))

    review = _review(api)
    placed = _checkout(api, key="new-generation")

    assert review is None
    assert placed.status_code == 200


def test_an_empty_cart_settles_the_old_order_before_a_guest_cart_is_merged(
    api, client, user, make_product, seed_cart
):
    bought = make_product(price="100.00", stock=10)
    guest_line = make_product(price="50.00", stock=10)
    seed_cart(user.id, {bought.id: 2}, version=1)
    old = _place_as_before(user, bought, 2, _cart(user).cart_id, 1)
    _old_clearing(user, bought)
    assert api.post("/api/cart/items", data=json.dumps({"product_id": guest_line.id})).status_code == 200
    api.force_login(user)

    cart = api.get("/api/cart").json()
    placed = _checkout(api, key="after-merge")

    assert _lines(cart) == [(guest_line.id, 1)] and cart["review_order"] is None
    assert Order.objects.get(pk=old).cart_clear_pending is False
    assert [(i["product_id"], i["quantity"]) for i in placed.json()["items"]] == [(guest_line.id, 1)]


def test_a_guest_cart_merged_into_a_cart_waiting_for_its_owner_waits_with_it(
    api, client, user, make_product, seed_cart
):
    bought = make_product(price="100.00", stock=3)
    guest_line = make_product(price="50.00", stock=10)
    key = seed_cart(user.id, {bought.id: 2}, version=1)
    old = _place_as_before(user, bought, 2, _cart(user).cart_id, 1)
    _as_before(cart_service.render, key, {bought.id: 2}, 1)
    assert api.post("/api/cart/items", data=json.dumps({"product_id": guest_line.id})).status_code == 200
    api.force_login(user)

    cart = api.get("/api/cart").json()
    refused = _checkout(api, key="after-merge")

    assert sorted(_lines(cart)) == sorted([(bought.id, 1), (guest_line.id, 1)])
    assert cart["review_order"] == old
    assert refused.status_code == 409 and refused.json()["code"] == "cart_needs_review"


def test_a_review_confirmed_for_a_version_the_cart_moved_past_is_refused(api, user, make_product, seed_cart):
    product = make_product(price="100.00", stock=3)
    key = seed_cart(user.id, {product.id: 2}, version=1)
    old = _place_as_before(user, product, 2, _cart(user).cart_id, 1)
    _as_before(cart_service.render, key, {product.id: 2}, 1)
    api.force_login(user)
    seen = api.get("/api/cart").json()["version"]
    api.post("/api/cart/items", data=json.dumps({"product_id": product.id}))

    stale = api.post("/api/cart/review", data=json.dumps({"expected_version": seen}))

    assert stale.status_code == 409
    assert stale.json()["cart"]["review_order"] == old

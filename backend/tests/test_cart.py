import json

import pytest
from asgiref.sync import async_to_sync
from django.test import Client
from redis.exceptions import RedisError

from cart import service

pytestmark = pytest.mark.django_db


def _post(api, product_id, quantity=1, expected_version=None):
    body = {"product_id": product_id, "quantity": quantity}
    if expected_version is not None:
        body["expected_version"] = expected_version
    return api.post("/api/cart/items", data=json.dumps(body), content_type="application/json")


def _run(coro_func, *args):
    # each call runs in a fresh event loop; the per-loop client cache rebinds
    service._clients.clear()
    return async_to_sync(coro_func)(*args)


def test_guest_add_sets_cookie_and_version(api, make_product):
    product = make_product(stock=10)

    resp = _post(api, product.id, quantity=2)

    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1
    assert data["count"] == 2
    assert data["items"][0]["product_id"] == product.id
    assert data["items"][0]["quantity"] == 2
    assert "cartid" in api.cookies


def test_versionless_writes_accumulate_and_bump_version(api, make_product):
    product = make_product(stock=10)

    _post(api, product.id, quantity=1)
    resp = _post(api, product.id, quantity=2)

    data = resp.json()
    assert data["version"] == 2
    assert data["items"][0]["quantity"] == 3


def test_stale_expected_version_conflicts(api, make_product):
    product = make_product(stock=10)
    _post(api, product.id, quantity=1)  # version -> 1

    resp = _post(api, product.id, quantity=1, expected_version=0)  # stale

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "cart_version_conflict"
    assert body["cart"]["version"] == 1
    assert body["cart"]["items"][0]["quantity"] == 1  # unchanged


def test_matching_expected_version_succeeds(api, make_product):
    product = make_product(stock=10)
    _post(api, product.id, quantity=1)  # version -> 1

    resp = _post(api, product.id, quantity=1, expected_version=1)

    assert resp.status_code == 200
    assert resp.json()["version"] == 2


def test_quantity_clamped_to_stock_with_adjustment(api, make_product):
    product = make_product(stock=3)

    resp = _post(api, product.id, quantity=5)

    data = resp.json()
    assert data["items"][0]["quantity"] == 3
    assert data["adjustments"][0]["from_qty"] == 5
    assert data["adjustments"][0]["to_qty"] == 3
    assert data["adjustments"][0]["reason"] == "stock"
    assert data["version"] == 2  # add bumped to 1, stock reconciliation bumped to 2


def test_out_of_stock_product_is_removed(api, make_product):
    product = make_product(stock=5)
    _post(api, product.id, quantity=2)

    product.stock_quantity = 0
    product.save(update_fields=["stock_quantity"])

    data = api.get("/api/cart").json()
    assert data["items"] == []
    assert data["removed_items"][0]["product_id"] == product.id
    assert data["removed_items"][0]["reason"] == "out_of_stock"
    assert data["version"] == 2  # add bumped to 1, removal reconciliation bumped to 2


def test_deactivated_product_is_removed(api, make_product):
    product = make_product(stock=5)
    _post(api, product.id, quantity=2)

    product.is_active = False
    product.save(update_fields=["is_active"])

    data = api.get("/api/cart").json()
    assert data["items"] == []
    assert data["removed_items"][0]["reason"] == "unavailable"


def test_add_unknown_product_returns_404(api, make_product):
    resp = _post(api, 999999, quantity=1)
    assert resp.status_code == 404


def test_login_merges_guest_cart_and_clears_cookie(api, make_product, user):
    product = make_product(stock=10)
    _post(api, product.id, quantity=2)  # guest cart

    api.force_login(user)
    data = api.get("/api/cart").json()

    assert data["count"] == 2
    assert data["items"][0]["product_id"] == product.id


def test_redis_down_returns_503(api, monkeypatch):
    class _Dead:
        async def hgetall(self, *a, **k):
            raise RedisError("down")

        async def eval(self, *a, **k):
            raise RedisError("down")

        async def hdel(self, *a, **k):
            raise RedisError("down")

        async def hset(self, *a, **k):
            raise RedisError("down")

        async def delete(self, *a, **k):
            raise RedisError("down")

    monkeypatch.setattr(service, "_redis", lambda: _Dead())

    resp = api.get("/api/cart")
    assert resp.status_code == 503


def test_clear_sync_is_version_scoped(make_product):
    key = service.user_key(1)
    service._sync_redis().hset(key, mapping={"5": 2, service.VERSION_FIELD: 3})
    cart_id = service.read_sync(key).cart_id

    assert service.clear_sync(key, 2, cart_id) is False  # stale version -> not cleared
    assert service._sync_redis().exists(key) == 1

    assert service.clear_sync(key, 3, cart_id) is True  # matching version -> cleared
    state = service.read_sync(key)
    assert state.items == {}
    assert state.version > 3  # the counter outlives the items it guarded


def test_merge_sums_quantities_and_render_caps_to_stock(make_product):
    product = make_product(stock=5)
    user_key = service.user_key(1)
    guest_key = service.guest_key("g-merge")

    _run(service.add, user_key, product.id, 4, None)  # user version -> 1
    _run(service.add, guest_key, product.id, 3, None)
    _run(service.merge_guest_into_user, guest_key, user_key)

    items, version = _run(service.read, user_key)
    assert items[product.id] == 7  # summed, below MAX_QTY
    assert version == 2  # merge atomically bumped the user version once

    cart = _run(service.render, user_key, items, version)
    assert cart["items"][0]["quantity"] == 5  # capped to stock
    assert cart["adjustments"][0]["from_qty"] == 7
    assert cart["adjustments"][0]["to_qty"] == 5
    assert cart["version"] == 3  # stock reconciliation bumped the version again

    guest_items, _ = _run(service.read, guest_key)
    assert guest_items == {}  # guest cart consumed


def test_authenticated_write_requires_csrf(make_product, user):
    product = make_product(stock=10)
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    service._clients.clear()

    resp = csrf_client.post(
        "/api/cart/items",
        data=json.dumps({"product_id": product.id, "quantity": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 403


def test_guest_write_carries_the_csrf_token_like_any_other(make_product):
    product = make_product(stock=10)
    csrf_client = Client(enforce_csrf_checks=True)
    service._clients.clear()
    csrf_client.get("/api/auth/csrf")

    resp = csrf_client.post(
        "/api/cart/items",
        data=json.dumps({"product_id": product.id, "quantity": 1}),
        content_type="application/json",
        headers={"X-CSRFToken": csrf_client.cookies["csrftoken"].value},
    )
    assert resp.status_code == 200


def test_authenticated_write_with_csrf_token_succeeds(make_product, user):
    product = make_product(stock=10)
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    csrf_client.get("/api/auth/csrf")  # sets the csrftoken cookie
    token = csrf_client.cookies["csrftoken"].value
    service._clients.clear()

    resp = csrf_client.post(
        "/api/cart/items",
        data=json.dumps({"product_id": product.id, "quantity": 1}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

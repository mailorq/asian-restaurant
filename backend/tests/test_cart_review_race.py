import json
import os
import threading

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test import Client

from cart import service as cart_service
from orders import service as order_service
from orders.models import Order
from tests.concurrency import WAIT_SECONDS, Writer
from tests.test_checkout_transition import _place_as_before


@pytest.fixture
def skip_claim(monkeypatch):
    # control: drop the atomic claim and keep the old read-check-update confirm. the version the
    # paused confirm read still matches, so it settles a cart that moved on - the finding returns
    if os.environ.get("AUDIT_SKIP_CLAIM") != "1":
        return
    monkeypatch.setattr(cart_service, "claim_sync", lambda key, expected, cart_id: expected + 1)


def _legacy_changed_cart(user, product, seed_cart):
    # a cart of the previous release, with its order, changed since that order to one unit
    key = seed_cart(user.id, {product.id: 2}, version=1)
    before = cart_service.read_sync(key)
    old = _place_as_before(user, product, 2, before.cart_id, before.version)
    cart_service._clients.clear()
    async_to_sync(cart_service.set_qty)(key, product.id, 1, None)
    return key, old


@pytest.mark.django_db(transaction=True)
def test_confirm_does_not_approve_a_cart_changed_after_it_read_the_version(
    api, user, make_product, seed_cart, skip_claim, monkeypatch
):
    product = make_product(price="100.00", stock=10)
    key, old = _legacy_changed_cart(user, product, seed_cart)
    api.force_login(user)
    seen = api.get("/api/cart").json()
    assert seen["review_order"] == old and seen["items"][0]["quantity"] == 1

    holding, release, responses = threading.Event(), threading.Event(), []
    read_original = cart_service.read_sync
    confirm_original = order_service.confirm_cart
    context = threading.local()

    def mark_confirmation(account, expected):
        context.confirming = True
        try:
            return confirm_original(account, expected)
        finally:
            context.confirming = False

    def hold_after_read(cart_key):
        snapshot = read_original(cart_key)
        if getattr(context, "confirming", False):
            holding.set()
            assert release.wait(WAIT_SECONDS)
        return snapshot

    monkeypatch.setattr(cart_service, "read_sync", hold_after_read)
    monkeypatch.setattr(order_service, "confirm_cart", mark_confirmation)

    confirm_client = Client()
    confirm_client.force_login(user)
    confirm = Writer(
        "confirm",
        lambda: responses.append(
            confirm_client.post(
                "/api/cart/review",
                data=json.dumps({"expected_version": seen["version"]}),
                content_type="application/json",
            )
        ),
    )
    connection.close()
    confirm.start()
    try:
        assert holding.wait(WAIT_SECONDS), "confirm never reached its read"
        changed = api.put(
            f"/api/cart/items/{product.pk}",
            data=json.dumps({"quantity": 3, "expected_version": seen["version"]}),
        )
        assert changed.status_code == 200 and changed.json()["version"] != seen["version"]
        assert changed.json()["review_order"] == old
    finally:
        release.set()
        confirm.join(WAIT_SECONDS)
    assert not confirm.is_alive() and confirm.error is None, repr(confirm.error)

    after = api.get("/api/cart").json()
    checkout = api.post(
        "/api/orders/checkout",
        data=json.dumps(
            {
                "address": "ул. Пушкина, 12",
                "payment_method": "cash",
                "idempotency_key": "unreviewed",
            }
        ),
    )
    assert responses[0].status_code == 409, "confirm approved a later, unseen cart version"
    assert after["review_order"] == old
    assert checkout.status_code == 409 and checkout.json()["code"] == "cart_needs_review"
    assert Order.objects.get(pk=old).cart_clear_pending is None


@pytest.mark.django_db(transaction=True)
def test_a_successful_confirm_bumps_the_version_so_a_stale_write_cannot_follow(
    api, user, make_product, seed_cart
):
    product = make_product(price="100.00", stock=10)
    key, old = _legacy_changed_cart(user, product, seed_cart)
    api.force_login(user)
    seen = api.get("/api/cart").json()

    confirmed = api.post("/api/cart/review", data=json.dumps({"expected_version": seen["version"]}))
    assert confirmed.status_code == 200
    assert confirmed.json()["review_order"] is None
    assert confirmed.json()["version"] != seen["version"]
    assert Order.objects.get(pk=old).cart_clear_pending is False

    # a write addressed to the version the owner confirmed no longer wins: it has moved on
    stale = api.put(
        f"/api/cart/items/{product.pk}",
        data=json.dumps({"quantity": 3, "expected_version": seen["version"]}),
    )
    assert stale.status_code == 409

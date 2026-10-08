import json
import os

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import AsyncClient, Client

from cart import service as cart_service
from config import middleware
from orders.models import Order

ADDRESS = "ул. Пушкина, 12"


@pytest.fixture(autouse=True)
def without_guard(monkeypatch):
    # control: without the guard a request is served for whoever is signed in, whatever account it names
    if os.environ.get("AUDIT_SKIP_ACCOUNT_GUARD") == "1":
        monkeypatch.setattr(middleware, "_account_changed", lambda claimed, user: None)


@pytest.fixture
def other(db):
    return get_user_model().objects.create_user(
        username="+79990000002", password="Pass!2345", first_name="Петр", phone="+79990000002"
    )


def _browser(*accounts) -> Client:
    # one browser, tabs sharing its cookies: each account signed in after the previous one
    browser = Client()
    for account in accounts:
        browser.force_login(account)
    return browser


def _send(client, method, path, account=None, body=None):
    cart_service._clients.clear()
    headers = {} if account is None else {"X-Account": account}
    if body is None:
        return getattr(client, method)(path, headers=headers)
    return getattr(client, method)(
        path, data=json.dumps(body), content_type="application/json", headers=headers
    )


def _items(account) -> dict[int, int]:
    return cart_service.read_sync(cart_service.user_key(account.pk)).items


def test_a_tab_showing_one_account_writes_nothing_for_the_account_signed_in_later(
    user, other, make_product
):
    product = make_product(stock=10)
    browser = _browser(user, other)
    added = _send(browser, "post", "/api/cart/items", str(user.pk), {"product_id": product.pk})
    assert added.status_code == 409 and added.json()["code"] == "account_changed"
    assert _items(other) == {} and _items(user) == {}


def test_a_tab_showing_one_account_places_no_order_for_another(
    user, other, make_product, seed_cart
):
    product = make_product(stock=10)
    seed_cart(other.pk, {product.pk: 2})
    browser = _browser(user, other)
    placed = _send(
        browser,
        "post",
        "/api/orders/checkout",
        str(user.pk),
        {"address": ADDRESS, "payment_method": "cash", "idempotency_key": "tab-a-checkout"},
    )
    assert placed.status_code == 409 and placed.json()["code"] == "account_changed"
    assert not Order.objects.filter(user=other).exists()
    assert _items(other) == {product.pk: 2}


@pytest.mark.parametrize("path", ["/api/orders", "/api/orders/address/last", "/api/cart"])
def test_a_tab_showing_one_account_reads_nothing_of_another(user, other, path):
    browser = _browser(user, other)
    read = _send(browser, "get", path, str(user.pk))
    assert read.status_code == 409 and read.json()["code"] == "account_changed"


def test_logout_and_the_employee_token_act_only_for_the_account_the_tab_shows(user, other):
    browser = _browser(user, other)
    assert _send(browser, "post", "/api/auth/logout", str(user.pk), {}).status_code == 409
    assert _send(browser, "post", "/api/auth/employee-token", str(user.pk), {}).status_code == 409
    me = _send(browser, "get", "/api/auth/me")
    assert me.status_code == 200 and me.json()["id"] == other.pk


def test_the_endpoints_that_find_out_or_change_who_is_signed_in_answer_any_tab(user, other):
    browser = _browser(user, other)
    me = _send(browser, "get", "/api/auth/me", str(user.pk))
    assert me.status_code == 200 and me.json()["id"] == other.pk
    guest = Client()
    signed = _send(
        guest,
        "post",
        "/api/auth/login",
        str(other.pk),
        {"phone": user.phone, "password": "Pass!2345"},
    )
    assert signed.status_code == 200 and signed.json()["id"] == user.pk


def test_a_request_for_the_account_signed_in_or_naming_none_is_served(user, other, make_product):
    product = make_product(stock=10)
    browser = _browser(user, other)
    named = _send(browser, "post", "/api/cart/items", str(other.pk), {"product_id": product.pk})
    unnamed = _send(browser, "post", "/api/cart/items", None, {"product_id": product.pk})
    assert named.status_code == 200 and unnamed.status_code == 200
    assert _items(other) == {product.pk: 2}
    assert _send(browser, "get", "/api/cart", "guest").status_code == 409

    guest = Client()
    assert _send(guest, "get", "/api/cart", "guest").status_code == 200
    assert _send(guest, "get", "/api/cart", str(user.pk)).status_code == 409


@pytest.mark.django_db(transaction=True)
def test_the_asgi_stack_refuses_and_serves_the_same_way(user, other, make_product):
    product = make_product(stock=10)
    browser = AsyncClient()
    browser.force_login(user)
    browser.force_login(other)

    def add(account):
        cart_service._clients.clear()
        return async_to_sync(browser.post)(
            "/api/cart/items",
            data=json.dumps({"product_id": product.pk}),
            content_type="application/json",
            headers={"X-Account": account},
        )

    refused = add(str(user.pk))
    assert refused.status_code == 409 and refused.json()["code"] == "account_changed"
    assert _items(other) == {}
    assert add(str(other.pk)).status_code == 200
    assert _items(other) == {product.pk: 1}

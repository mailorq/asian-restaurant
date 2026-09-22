import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db


def _client(user):
    c = Client(enforce_csrf_checks=True)
    c.force_login(user)
    return c


def _post_json(client, url):
    return client.post(url, data="{}", content_type="application/json")


# every session-authenticated mutation must reject a request with no CSRF token
def test_checkout_enforces_csrf(user):
    assert _post_json(_client(user), "/api/orders/checkout").status_code == 403


def test_address_verify_enforces_csrf(user):
    assert _post_json(_client(user), "/api/orders/address/verify").status_code == 403


def test_employee_order_transition_enforces_csrf(employee_user):
    assert _post_json(_client(employee_user), "/api/employee/orders/1/transition").status_code == 403


def test_inventory_adjust_enforces_csrf(employee_user):
    assert _post_json(_client(employee_user), "/api/employee/inventory/1/adjust").status_code == 403


def test_role_change_enforces_csrf(employee_user):
    assert _post_json(_client(employee_user), "/api/employee/users/1/role").status_code == 403


# the site's own pages send json with the csrf token, signed in or not; a page elsewhere can do neither
def _anonymous():
    return Client(enforce_csrf_checks=True)


def _with_token(client):
    client.get("/api/auth/csrf")
    return {"X-CSRFToken": client.cookies["csrftoken"].value}


def test_a_cross_site_text_plain_login_is_refused(user):
    client = _anonymous()
    body = json.dumps({"phone": user.phone, "password": "Pass!2345", "p": "="})

    response = client.post("/api/auth/login", data=body, content_type="text/plain")

    assert response.status_code == 403
    assert "_auth_user_id" not in client.session


def test_a_login_without_a_token_is_refused(user):
    client = _anonymous()

    response = client.post(
        "/api/auth/login",
        data=json.dumps({"phone": user.phone, "password": "Pass!2345"}),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert "_auth_user_id" not in client.session


def test_a_registration_without_a_token_is_refused():
    payload = {"phone": "+79990002222", "password": "Pass!2345word", "name": "Гость"}

    response = _anonymous().post(
        "/api/auth/register", data=json.dumps(payload), content_type="application/json"
    )

    assert response.status_code == 403
    assert not get_user_model().objects.filter(phone="+79990002222").exists()


def test_an_anonymous_cart_write_without_a_token_is_refused(make_product):
    product = make_product(stock=5)
    client = _anonymous()

    response = client.post(
        "/api/cart/items",
        data=json.dumps({"product_id": product.id}),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert "cartid" not in response.cookies


def test_a_write_with_a_token_but_not_a_json_body_is_unsupported(user):
    client = _anonymous()
    headers = _with_token(client)
    body = json.dumps({"phone": user.phone, "password": "Pass!2345"})

    response = client.post("/api/auth/login", data=body, content_type="text/plain", headers=headers)

    assert response.status_code == 415
    assert "_auth_user_id" not in client.session


def test_the_site_own_json_writes_with_a_token_pass(user, make_product):
    product = make_product(stock=5)
    client = _anonymous()
    headers = _with_token(client)

    added = client.post(
        "/api/cart/items",
        data=json.dumps({"product_id": product.id}),
        content_type="application/json",
        headers=headers,
    )
    logged_in = client.post(
        "/api/auth/login",
        data=json.dumps({"phone": user.phone, "password": "Pass!2345"}),
        content_type="application/json",
        headers=headers,
    )

    assert added.status_code == 200
    assert logged_in.status_code == 200

import json

import pytest

from accounts.models import EmployeeRoleAudit
from menu.models import StockAdjustment
from orders.models import Order, OrderOutbox

pytestmark = pytest.mark.django_db


@pytest.fixture
def sample_order(client, user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    client.force_login(user)
    resp = client.post(
        "/api/orders/checkout",
        data=json.dumps({"address": "ул. Пушкина, 12", "payment_method": "cash", "idempotency_key": "idem-sample-1"}),
        content_type="application/json",
    )
    order = Order.objects.get(pk=resp.json()["id"])
    client.logout()
    return order


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


# access control
def test_guest_gets_401(client):
    assert client.get("/api/employee/orders").status_code == 401


def test_regular_user_gets_403(client, user):
    client.force_login(user)
    assert client.get("/api/employee/orders").status_code == 403


def test_is_staff_alone_does_not_grant(client, django_user_model):
    staff = django_user_model.objects.create_user(username="+79990000099", password="x", is_staff=True)
    client.force_login(staff)
    assert client.get("/api/employee/orders").status_code == 403


def test_employee_and_superuser_have_access(client, employee_user, superuser):
    client.force_login(employee_user)
    assert client.get("/api/employee/orders").status_code == 200
    client.force_login(superuser)
    assert client.get("/api/employee/orders").status_code == 200


def test_me_reports_is_employee(client, employee_user):
    client.force_login(employee_user)
    assert client.get("/api/auth/me").json()["is_employee"] is True


# orders
def test_transition_updates_status_and_emits_event(client, employee_user, sample_order):
    client.force_login(employee_user)
    resp = _post(client, f"/api/employee/orders/{sample_order.id}/transition", {"to_status": "confirmed", "note": "ок"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    sample_order.refresh_from_db()
    assert sample_order.status == "confirmed"
    assert OrderOutbox.objects.filter(aggregate_id=str(sample_order.id), event_type="order.status_changed").exists()
    assert sample_order.history.filter(to_status="confirmed", changed_by=employee_user).exists()


def test_invalid_transition_returns_400(client, employee_user, sample_order):
    client.force_login(employee_user)
    resp = _post(client, f"/api/employee/orders/{sample_order.id}/transition", {"to_status": "delivered"})
    assert resp.status_code == 400  # created -> delivered is not allowed


# inventory
def test_inventory_adjust_records_stock_adjustment(client, employee_user, make_product):
    product = make_product(stock=5)
    client.force_login(employee_user)
    resp = _post(client, f"/api/employee/inventory/{product.id}/adjust", {"new_quantity": 12, "reason": "поставка", "expected_version": product.version})
    assert resp.status_code == 200
    assert resp.json()["stock_quantity"] == 12
    product.refresh_from_db()
    assert product.stock_quantity == 12
    adjustment = StockAdjustment.objects.filter(product=product).first()
    assert adjustment.old_quantity == 5
    assert adjustment.new_quantity == 12
    assert adjustment.staff_id == employee_user.id
    assert adjustment.reason == "поставка"


def test_inventory_search(client, employee_user, make_product):
    make_product(name="Рамен", stock=1)
    make_product(name="Гёдза", stock=1)
    client.force_login(employee_user)
    names = [i["name"] for i in client.get("/api/employee/inventory?search=рамен").json()]
    assert names == ["Рамен"]


# users
def test_users_list_paginated_with_active_orders_count(client, employee_user, sample_order):
    client.force_login(employee_user)
    data = client.get("/api/employee/users?page=1&page_size=50").json()
    assert data["page"] == 1 and data["total"] >= 1
    customer = next(u for u in data["items"] if u["id"] == sample_order.user_id)
    assert customer["active_orders_count"] == 1


def test_user_detail_previews_order_history(client, employee_user, sample_order):
    client.force_login(employee_user)
    resp = client.get(f"/api/employee/users/{sample_order.user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["orders_preview"][0]["id"] == sample_order.id
    assert body["orders_total"] == 1


# role management
def test_role_change_forbidden_for_employee(client, employee_user, user):
    client.force_login(employee_user)  # employee, but not superuser
    resp = _post(client, f"/api/employee/users/{user.id}/role", {"role": "restaurant_manager"})
    assert resp.status_code == 403


def test_superuser_grants_and_revokes_with_audit(client, superuser, user):
    client.force_login(superuser)

    granted = _post(client, f"/api/employee/users/{user.id}/role", {"role": "restaurant_manager"})
    assert granted.status_code == 200
    assert granted.json()["is_employee"] is True
    assert EmployeeRoleAudit.objects.filter(target=user, actor=superuser, action="grant").exists()

    revoked = _post(client, f"/api/employee/users/{user.id}/role", {"role": None})
    assert revoked.json()["is_employee"] is False
    assert EmployeeRoleAudit.objects.filter(target=user, action="revoke").exists()

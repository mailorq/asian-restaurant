from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib import admin as dj_admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory

from accounts.admin import CustomUserAdmin
from accounts.models import User
from menu.admin import ProductAdmin, StockAdjustmentAdmin
from menu.models import Product, StockAdjustment
from orders.admin import DeliveryAddressAdmin, OrderAdmin, _make_transition_action
from orders.models import DeliveryAddress, Order, OrderItem, OrderOutbox, OrderStatusHistory

pytestmark = pytest.mark.django_db


def _req(user):
    return SimpleNamespace(user=user)


def _msg_request(user):
    req = RequestFactory().post("/")
    req.user = user
    req.session = {}
    req._messages = FallbackStorage(req)
    return req


def _make_order(user, product):
    addr = DeliveryAddress.objects.create(user=user, address="ул. 1", is_verified=True)
    order = Order.objects.create(
        user=user, status="created", payment_method="cash", phone="+70000000000",
        contact_name="Иван", delivery_address=addr, total=Decimal("100.00"),
        idempotency_key="admin-test", source_cart_id="admin", source_cart_version=1,
    )
    OrderItem.objects.create(order=order, product=product, product_name=product.name,
                             unit_price=Decimal("50.00"), quantity=2, line_total=Decimal("100.00"))
    OrderStatusHistory.objects.create(order=order, from_status="", to_status="created", note="seed")
    return order


def test_product_admin_stock_edit_routes_through_service(make_product, employee_user):
    product = make_product(stock=5)
    ma = ProductAdmin(Product, dj_admin.site)
    product.stock_quantity = 20  # what the admin form submitted
    form = SimpleNamespace(
        changed_data=["stock_quantity"], cleaned_data={"expected_version": product.version}
    )
    ma.save_model(_req(employee_user), product, form, change=True)

    product.refresh_from_db()
    assert product.stock_quantity == 20 and product.version == 2
    assert OrderOutbox.objects.filter(event_type="inventory.stock_changed", aggregate_id=product.code).exists()
    assert StockAdjustment.objects.filter(product=product, reason="admin edit").exists()


def test_product_admin_non_projected_edit_emits_nothing(make_product, employee_user):
    product = make_product(stock=5)
    ma = ProductAdmin(Product, dj_admin.site)
    product.description = "новое описание"  # not a field operations projects
    ma.save_model(_req(employee_user), product, SimpleNamespace(changed_data=["description"]), change=True)

    product.refresh_from_db()
    assert product.version == 1
    assert not OrderOutbox.objects.filter(event_type="inventory.stock_changed").exists()


def test_product_admin_rename_bumps_version_and_emits_event(make_product, employee_user):
    product = make_product(stock=5)
    start = product.version
    ma = ProductAdmin(Product, dj_admin.site)
    product.name = "Рамен Делюкс"
    form = SimpleNamespace(
        changed_data=["name"], cleaned_data={"expected_version": product.version}
    )
    ma.save_model(_req(employee_user), product, form, change=True)

    product.refresh_from_db()
    assert product.name == "Рамен Делюкс" and product.version == start + 1
    row = OrderOutbox.objects.filter(event_type="inventory.stock_changed", aggregate_id=product.code).latest("created_at")
    assert row.payload["name"] == "Рамен Делюкс"


def test_user_admin_profile_edit_routes_through_service(user):
    ma = CustomUserAdmin(User, dj_admin.site)
    user.first_name = "Пётр"
    ma.save_model(_req(user), user, SimpleNamespace(changed_data=["first_name"]), change=True)

    user.refresh_from_db()
    assert user.first_name == "Пётр" and user.customer_version == 2
    assert OrderOutbox.objects.filter(event_type="identity.customer_changed", aggregate_id=str(user.id)).exists()


def test_stock_adjustment_admin_is_audit_only():
    ma = StockAdjustmentAdmin(StockAdjustment, dj_admin.site)
    req = _req(None)
    assert ma.has_add_permission(req) is False
    assert ma.has_change_permission(req) is False
    assert ma.has_delete_permission(req) is False


def test_admin_created_product_emits_live_event():
    ma = ProductAdmin(Product, dj_admin.site)
    p = Product(code="admin_new", category="dish", name="Новый", price=Decimal("10.00"), stock_quantity=7)
    ma.save_model(_req(None), p, SimpleNamespace(changed_data=[]), change=False)
    assert OrderOutbox.objects.filter(event_type="inventory.stock_changed", aggregate_id="admin_new").exists()


def test_admin_created_user_emits_live_event():
    ma = CustomUserAdmin(User, dj_admin.site)
    u = User(username="+79991112233", first_name="Новый")
    ma.save_model(_req(None), u, SimpleNamespace(changed_data=[]), change=False)
    assert OrderOutbox.objects.filter(event_type="identity.customer_changed", aggregate_id=str(u.id)).exists()


def test_order_admin_is_view_only_and_undeletable():
    ma = OrderAdmin(Order, dj_admin.site)
    assert ma.has_add_permission(_req(None)) is False
    assert ma.has_delete_permission(_req(None)) is False
    for field in ("status", "total", "contact_name", "phone", "delivery_address"):
        assert field in ma.readonly_fields


def test_delivery_address_admin_is_immutable():
    ma = DeliveryAddressAdmin(DeliveryAddress, dj_admin.site)
    req = _req(None)
    assert ma.has_add_permission(req) is False
    assert ma.has_change_permission(req) is False
    assert ma.has_delete_permission(req) is False


def test_product_and_user_admin_forbid_hard_delete():
    assert ProductAdmin(Product, dj_admin.site).has_delete_permission(_req(None)) is False
    assert CustomUserAdmin(User, dj_admin.site).has_delete_permission(_req(None)) is False


def test_order_admin_transition_action_routes_through_service(user, make_product, employee_user):
    product = make_product(stock=10)
    order = _make_order(user, product)
    ma = OrderAdmin(Order, dj_admin.site)

    _make_transition_action("confirmed")(ma, _msg_request(employee_user), Order.objects.filter(pk=order.pk))

    order.refresh_from_db()
    assert order.status == "confirmed"
    assert OrderStatusHistory.objects.filter(order=order, to_status="confirmed").exists()
    assert OrderOutbox.objects.filter(event_type="order.status_changed", aggregate_id=str(order.id)).exists()

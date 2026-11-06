import pytest

from accounts import service as accounts_service
from menu import inventory
from menu.models import StockAdjustment
from orders.models import OrderOutbox

pytestmark = pytest.mark.django_db


def _events(event_type):
    return OrderOutbox.objects.filter(event_type=event_type)


def test_set_stock_bumps_version_and_emits_event(make_product):
    product = make_product(stock=50)
    start = product.version

    inventory.set_stock(product.id, 40, reason="restock")

    product.refresh_from_db()
    assert product.stock_quantity == 40 and product.version == start + 1
    row = _events("inventory.stock_changed").latest("created_at")
    assert row.aggregate_id == product.code
    assert row.aggregate_version == product.version
    assert row.payload == {"product_code": product.code, "name": product.name, "stock_quantity": 40}
    assert row.snapshot is False


def test_set_stock_no_change_emits_nothing(make_product):
    product = make_product(stock=50)
    inventory.set_stock(product.id, 50, reason="noop")
    assert not _events("inventory.stock_changed").exists()


def test_snapshot_emit_sets_flag(make_product):
    product = make_product(stock=7)
    inventory.emit_state(product, snapshot=True)
    row = _events("inventory.stock_changed").latest("created_at")
    assert row.snapshot is True and row.aggregate_version == product.version


def test_customer_profile_change_bumps_version_and_emits(user):
    start = user.customer_version

    accounts_service.set_customer_profile(user.id, name="Пётр", phone="+79990000009")

    user.refresh_from_db()
    assert user.first_name == "Пётр" and user.customer_version == start + 1
    row = _events("identity.customer_changed").latest("created_at")
    assert row.aggregate_id == str(user.id)
    assert row.payload == {"customer_id": user.id, "name": "Пётр", "phone": "+79990000009"}


def test_customer_profile_no_change_is_silent(user):
    accounts_service.set_customer_profile(user.id, name=user.first_name)
    assert not _events("identity.customer_changed").exists()


def test_record_stock_change_emits_and_audits(make_product):
    # the exact function checkout uses for each ordered item
    product = make_product(stock=5)
    start = product.version

    inventory.record_stock_change(product, 3, reason="order #1", source=StockAdjustment.Source.SALE)

    product.refresh_from_db()
    assert product.stock_quantity == 3 and product.version == start + 1
    assert StockAdjustment.objects.filter(product=product, reason="order #1", source="sale").exists()
    assert _events("inventory.stock_changed").latest("created_at").payload["stock_quantity"] == 3

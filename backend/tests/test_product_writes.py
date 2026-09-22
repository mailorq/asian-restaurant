import json
import threading
from types import SimpleNamespace

import pytest
from django.contrib import admin as dj_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from accounts.roles import StaffRole
from cart import service as cart_service
from menu import inventory
from menu.admin import ProductAdmin
from menu.models import Product, StockAdjustment
from orders import service as order_service
from orders.models import OrderOutbox
from tests.admin_forms import rendered_form
from tests.concurrency import await_a_lock_waiter, in_thread

ADDRESS = "ул. Пушкина, 12"


def _sell(product, quantity, n):
    buyer = get_user_model().objects.create_user(
        username=f"+7999000{n:04d}", password="Pass!2345", phone=f"+7999000{n:04d}"
    )
    cart_service._sync_redis().hset(
        cart_service.user_key(buyer.id),
        mapping={str(product.id): quantity, cart_service.VERSION_FIELD: 1},
    )
    return order_service.checkout(buyer, ADDRESS, "cash", f"idem-sale-{n}")


def _event_versions(product) -> list[int]:
    return list(
        OrderOutbox.objects.filter(
            event_type="inventory.stock_changed", aggregate_id=product.code
        ).values_list("aggregate_version", flat=True)
    )


@pytest.mark.django_db
def test_a_price_edit_from_a_form_opened_before_a_sale_keeps_the_sale(admin_client, make_product):
    product = make_product(price="100.00", stock=10)
    form = rendered_form(admin_client, f"/admin/menu/product/{product.pk}/change/")
    _sell(product, 3, 1)

    form["price"] = "555.00"
    response = admin_client.post(f"/admin/menu/product/{product.pk}/change/", form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert str(product.price) == "555.00"
    assert product.stock_quantity == 7
    assert product.version == 2
    assert not StockAdjustment.objects.filter(product=product, reason="admin edit").exists()


@pytest.mark.django_db
def test_a_stock_edit_from_a_form_opened_before_a_sale_is_refused(admin_client, make_product):
    product = make_product(stock=10)
    form = rendered_form(admin_client, f"/admin/menu/product/{product.pk}/change/")
    _sell(product, 3, 2)

    form["stock_quantity"] = "12"
    response = admin_client.post(f"/admin/menu/product/{product.pk}/change/", form)

    assert response.status_code == 200
    assert "Товар изменился" in response.content.decode()
    product.refresh_from_db()
    assert product.stock_quantity == 7
    assert not StockAdjustment.objects.filter(product=product, reason="admin edit").exists()


@pytest.mark.django_db
def test_a_stock_edit_from_a_current_form_is_applied_once(admin_client, make_product):
    product = make_product(stock=10)
    form = rendered_form(admin_client, f"/admin/menu/product/{product.pk}/change/")

    form["stock_quantity"] = "12"
    response = admin_client.post(f"/admin/menu/product/{product.pk}/change/", form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert (product.stock_quantity, product.version) == (12, 2)
    assert (
        StockAdjustment.objects.filter(
            product=product, reason="admin edit", new_quantity=12
        ).count()
        == 1
    )
    assert _event_versions(product) == [2]


@pytest.mark.django_db
def test_saving_an_object_loaded_before_a_sale_touches_only_the_edited_field(
    make_product, superuser
):
    product = make_product(price="100.00", stock=10)
    loaded = Product.objects.get(pk=product.pk)
    _sell(product, 3, 3)

    loaded.price = "120.00"
    form = SimpleNamespace(changed_data=["price"], cleaned_data={"price": loaded.price})
    ProductAdmin(Product, dj_admin.site).save_model(
        SimpleNamespace(user=superuser), loaded, form, change=True
    )

    product.refresh_from_db()
    assert str(product.price) == "120.00"
    assert (product.stock_quantity, product.version) == (7, 2)
    versions = _event_versions(product)
    assert len(versions) == len(set(versions)), f"two events claim one version: {versions}"


def _adjust(client, product, quantity, expected_version):
    return client.post(
        f"/api/employee/inventory/{product.id}/adjust",
        data=json.dumps(
            {
                "new_quantity": quantity,
                "reason": "инвентаризация",
                "expected_version": expected_version,
            }
        ),
        content_type="application/json",
    )


@pytest.mark.django_db
def test_an_adjustment_against_a_stale_version_is_refused_with_the_current_product(
    client, employee_user, make_product
):
    product = make_product(stock=10)
    seen = product.version
    _sell(product, 3, 4)
    client.force_login(employee_user)

    response = _adjust(client, product, 20, seen)

    assert response.status_code == 409
    body = response.json()
    assert body["product"]["stock_quantity"] == 7
    assert body["product"]["version"] == seen + 1
    product.refresh_from_db()
    assert product.stock_quantity == 7


@pytest.mark.django_db
def test_an_adjustment_against_the_current_version_returns_the_next_one(
    client, employee_user, make_product
):
    product = make_product(stock=10)
    client.force_login(employee_user)

    response = _adjust(client, product, 20, product.version)

    assert response.status_code == 200
    assert (response.json()["stock_quantity"], response.json()["version"]) == (
        20,
        product.version + 1,
    )


def _staff():
    account = get_user_model().objects.create_user(username="+79990007001", password="Pass!2345")
    account.groups.add(Group.objects.get_or_create(name=StaffRole.MANAGER)[0])
    return account


@pytest.mark.django_db(transaction=True)
def test_a_correction_queued_behind_a_sale_is_refused_after_it(monkeypatch):
    staff = _staff()
    product = Product.objects.create(
        code="race_sale_first",
        category="dish",
        name="Рамен",
        description="d",
        price="100.00",
        stock_quantity=10,
        is_active=True,
    )
    seen = product.version
    holding, release, errors, outcome = threading.Event(), threading.Event(), [], {}
    real = inventory.record_stock_change

    def sale_holds_the_row(*args, **kwargs):
        real(*args, **kwargs)
        holding.set()
        assert release.wait(timeout=20)

    monkeypatch.setattr(inventory, "record_stock_change", sale_holds_the_row)

    def correct():
        try:
            inventory.set_stock(
                product.id, 20, reason="инвентаризация", staff=staff, expected_version=seen
            )
            outcome["correction"] = "applied"
        except inventory.StaleProduct:
            outcome["correction"] = "refused"

    sale = in_thread("sale", lambda: _sell(product, 3, 201), errors)
    correction = in_thread("correction", correct, errors)
    sale.start()
    assert holding.wait(timeout=15)
    correction.start()
    await_a_lock_waiter()
    release.set()
    sale.join(timeout=30)
    correction.join(timeout=30)

    assert not errors, errors
    assert outcome == {"correction": "refused"}
    product.refresh_from_db()
    assert product.stock_quantity == 7
    assert sorted(_event_versions(product)) == [seen + 1]


@pytest.mark.django_db(transaction=True)
def test_a_sale_queued_behind_a_correction_is_taken_from_the_corrected_stock(monkeypatch):
    staff = _staff()
    product = Product.objects.create(
        code="race_correction_first",
        category="dish",
        name="Рамен",
        description="d",
        price="100.00",
        stock_quantity=10,
        is_active=True,
    )
    seen = product.version
    holding, release, errors = threading.Event(), threading.Event(), []
    real = inventory._emit

    def correction_holds_the_row(target, **kwargs):
        real(target, **kwargs)
        if threading.current_thread().name == "correction":
            holding.set()
            assert release.wait(timeout=20)

    monkeypatch.setattr(inventory, "_emit", correction_holds_the_row)

    correction = in_thread(
        "correction",
        lambda: inventory.set_stock(
            product.id, 20, reason="инвентаризация", staff=staff, expected_version=seen
        ),
        errors,
    )
    sale = in_thread("sale", lambda: _sell(product, 3, 202), errors)
    correction.start()
    assert holding.wait(timeout=15)
    sale.start()
    await_a_lock_waiter()
    release.set()
    correction.join(timeout=30)
    sale.join(timeout=30)

    assert not errors, errors
    product.refresh_from_db()
    assert product.stock_quantity == 17
    assert sorted(_event_versions(product)) == [seen + 1, seen + 2]


@pytest.mark.django_db
def test_an_image_uploaded_in_the_admin_is_stored_and_kept(
    admin_client, make_product, settings, tmp_path
):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    settings.MEDIA_ROOT = tmp_path
    product = make_product(stock=10)
    url = f"/admin/menu/product/{product.pk}/change/"
    form = rendered_form(admin_client, url)
    png = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(png, "PNG")
    form["image"] = SimpleUploadedFile("ramen.png", png.getvalue(), content_type="image/png")

    response = admin_client.post(url, form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert product.image.name.startswith("products/ramen")
    assert (tmp_path / product.image.name).read_bytes() == png.getvalue()
    assert (product.stock_quantity, product.version) == (10, 1)


@pytest.mark.django_db
def test_the_code_operations_keys_a_product_by_cannot_be_changed_in_the_admin(
    admin_client, make_product
):
    product = make_product(stock=10)
    url = f"/admin/menu/product/{product.pk}/change/"
    form = rendered_form(admin_client, url)
    form["code"] = "renamed"

    response = admin_client.post(url, form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert product.code != "renamed"


@pytest.mark.django_db
def test_the_stock_writer_refuses_a_code_change(make_product):
    product = make_product(stock=10)

    with pytest.raises(ValueError):
        inventory.update_product(product.id, {"code": "renamed"}, reason="rename")

    product.refresh_from_db()
    assert product.code != "renamed"

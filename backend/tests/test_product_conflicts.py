import pytest
from django.contrib.auth import get_user_model

from cart import service as cart_service
from menu import inventory
from menu.models import Product, ProductChange, StockAdjustment
from orders import service as order_service
from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db

LIST = "/admin/menu/product/"


def _card(product) -> str:
    return f"/admin/menu/product/{product.pk}/change/"


@pytest.fixture
def colleague():
    return get_user_model().objects.create_user(username="kitchen_lead", password="Pass!2345")


def _sell(product, quantity, n):
    buyer = get_user_model().objects.create_user(
        username=f"buyer_{n}", password="Pass!2345", phone="+380671111111"
    )
    cart_service._sync_redis().hset(
        cart_service.user_key(buyer.id),
        mapping={str(product.id): quantity, cart_service.VERSION_FIELD: 1},
    )
    return order_service.checkout(buyer, "ул. Пушкина, 12", "cash", f"idem-conflict-{n}")


def test_a_price_changed_while_the_card_was_open_comes_back_with_its_value_and_author(
    admin_client, make_product, colleague
):
    product = make_product(price="100.00")
    form = rendered_form(admin_client, _card(product))
    inventory.update_product(product.pk, {"price": "120.00"}, reason="admin edit", staff=colleague)

    form["price"] = "150.00"
    response = admin_client.post(_card(product), form)

    assert response.status_code == 200
    page = response.content.decode()
    assert "сейчас «120.00»" in page and "kitchen_lead" in page and "у вас «150.00»" in page
    product.refresh_from_db()
    assert str(product.price) == "120.00"


def test_keeping_the_current_value_drops_only_that_edit(admin_client, make_product, colleague):
    product = make_product(price="100.00")
    form = rendered_form(admin_client, _card(product))
    inventory.update_product(product.pk, {"price": "120.00"}, reason="admin edit", staff=colleague)
    form.update({"price": "150.00", "description": "новое описание"})
    admin_client.post(_card(product), form)

    form.update({"resolve-price": "keep", "shown-price": "120.00"})
    response = admin_client.post(_card(product), form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert (str(product.price), product.description) == ("120.00", "новое описание")


def test_writing_over_the_shown_value_saves_the_edit_and_signs_it(
    admin_client, superuser, make_product, colleague
):
    product = make_product(price="100.00")
    form = rendered_form(admin_client, _card(product))
    inventory.update_product(product.pk, {"price": "120.00"}, reason="admin edit", staff=colleague)

    form.update({"price": "150.00", "resolve-price": "mine", "shown-price": "120.00"})
    response = admin_client.post(_card(product), form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert str(product.price) == "150.00"
    latest = ProductChange.objects.filter(product=product, field="price").first()
    assert (latest.old_value, latest.new_value, latest.staff) == ("120.00", "150.00", superuser)


def test_writing_over_is_refused_when_the_value_moved_again_after_it_was_shown(
    admin_client, make_product, colleague
):
    product = make_product(price="100.00")
    form = rendered_form(admin_client, _card(product))
    inventory.update_product(product.pk, {"price": "120.00"}, reason="admin edit", staff=colleague)
    inventory.update_product(product.pk, {"price": "130.00"}, reason="admin edit", staff=colleague)

    form.update({"price": "150.00", "resolve-price": "mine", "shown-price": "120.00"})
    response = admin_client.post(_card(product), form)

    assert response.status_code == 200
    assert "сейчас «130.00»" in response.content.decode()
    product.refresh_from_db()
    assert str(product.price) == "130.00"


def test_a_rename_from_a_card_opened_before_a_sale_is_written_and_keeps_the_sale(
    admin_client, make_product
):
    product = make_product(name="Рамен", stock=10)
    form = rendered_form(admin_client, _card(product))
    _sell(product, 3, 1)

    form["name"] = "Рамен Делюкс"
    response = admin_client.post(_card(product), form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert (product.name, product.stock_quantity, product.version) == ("Рамен Делюкс", 7, 3)


def test_a_stock_moved_by_a_sale_is_shown_as_a_sale_not_as_a_person(admin_client, make_product):
    product = make_product(stock=10)
    form = rendered_form(admin_client, _card(product))
    _sell(product, 3, 2)

    form["stock_quantity"] = "12"
    response = admin_client.post(_card(product), form)

    assert response.status_code == 200
    page = response.content.decode()
    assert "сейчас «7» (продажа" in page
    product.refresh_from_db()
    assert product.stock_quantity == 7


def test_stock_returned_by_a_cancelled_order_is_signed_by_no_one(
    admin_client, client, make_product, employee_user
):
    product = make_product(stock=10)
    order = _sell(product, 3, 3)
    form = rendered_form(admin_client, _card(product))
    order_service.transition(order, "cancelled", changed_by=employee_user)

    form["stock_quantity"] = "12"
    page = admin_client.post(_card(product), form).content.decode()

    assert "сейчас «10» (отмена заказа" in page and employee_user.username not in page
    returned = StockAdjustment.objects.filter(product=product).first()
    assert (returned.source, returned.staff_id) == (StockAdjustment.Source.CANCELLATION, None)
    client.force_login(employee_user)
    history = client.get(f"/api/employee/inventory/{product.pk}/adjustments").json()
    assert [row["staff"] for row in history] == [None, None]


@pytest.mark.parametrize(
    ("reason", "by_staff", "author"),
    [
        ("cancelled order #7", True, "отмена заказа"),
        ("order #7", False, "продажа"),
        ("поставка", True, None),
        ("initial stock", False, "система"),
    ],
)
def test_a_stock_row_the_release_before_wrote_is_signed_by_its_reason(
    client, make_product, employee_user, reason, by_staff, author
):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    product = make_product(stock=10)
    previous = (
        MigrationExecutor(connection).loader.project_state([("menu", "0003_product_version")]).apps
    )
    # written while this release migrated: the source it does not know is left to the database default
    previous.get_model("menu", "StockAdjustment").objects.create(
        product_id=product.pk,
        staff_id=employee_user.pk if by_staff else None,
        old_quantity=7,
        new_quantity=10,
        reason=reason,
    )

    assert StockAdjustment.objects.get(product=product).source == ""
    signed = author or employee_user.username
    assert inventory.author_of(product, "stock_quantity")[0] == signed
    client.force_login(employee_user)
    history = client.get(f"/api/employee/inventory/{product.pk}/adjustments").json()
    assert [row["staff"] for row in history] == [None if author else employee_user.username]


@pytest.mark.django_db(transaction=True)
def test_a_card_saved_while_the_list_writes_waits_for_it_and_then_sees_its_edit(
    admin_client, make_product, colleague, monkeypatch
):
    import threading

    from menu.admin import ProductAdmin
    from tests.concurrency import Writer, queue_behind

    product = make_product(price="100.00")
    form = rendered_form(admin_client, LIST)
    form.update({"form-0-price": "130.00", "_save": "Save"})
    holding, release, outcome = threading.Event(), threading.Event(), {}
    real = ProductAdmin.save_model

    # the list has checked its row and is about to write it
    def list_holds(self, request, obj, form, change):
        holding.set()
        assert release.wait(timeout=20)
        real(self, request, obj, form, change)

    monkeypatch.setattr(ProductAdmin, "save_model", list_holds)

    def card():
        try:
            inventory.update_product(
                product.pk,
                {"price": "140.00"},
                reason="admin edit",
                staff=colleague,
                seen={"price": "100.00"},
            )
        except inventory.ProductConflict as exc:
            outcome["card"] = exc.conflicts[0].current

    lister = Writer("list", lambda: outcome.setdefault("list", admin_client.post(LIST, form)))
    met = queue_behind(lister, Writer("card", card), holding, release)

    assert met == "blocked"
    assert outcome["list"].status_code == 302
    product.refresh_from_db()
    assert (str(product.price), str(outcome["card"])) == ("130.00", "130.00")


def test_a_change_nobody_recorded_has_no_author(admin_client, make_product):
    product = make_product()
    form = rendered_form(admin_client, _card(product))
    Product.objects.filter(pk=product.pk).update(description="чужое")

    form["description"] = "мое"
    page = admin_client.post(_card(product), form).content.decode()

    assert "сейчас «чужое» (автор неизвестен)" in page


def test_a_photo_replaced_meanwhile_is_no_conflict_for_an_edit_of_another_field(
    admin_client, make_product, colleague
):
    product = make_product(price="100.00")
    Product.objects.filter(pk=product.pk).update(image="products/old.png")
    form = rendered_form(admin_client, _card(product))
    inventory.update_product(
        product.pk, {"image": "products/new.png"}, reason="admin edit", staff=colleague
    )

    form["price"] = "150.00"
    response = admin_client.post(_card(product), form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert (str(product.price), product.image.name) == ("150.00", "products/new.png")


def test_the_list_refuses_a_price_changed_while_it_was_open_and_points_to_the_card(
    admin_client, make_product, colleague
):
    product = make_product(price="100.00")
    form = rendered_form(admin_client, LIST)
    inventory.update_product(product.pk, {"price": "120.00"}, reason="admin edit", staff=colleague)

    form.update({"form-0-price": "130.00", "_save": "Save"})
    response = admin_client.post(LIST, form)

    assert response.status_code == 200
    page = response.content.decode()
    assert "сейчас «120.00» (kitchen_lead" in page and "Решите в карточке товара" in page
    product.refresh_from_db()
    assert str(product.price) == "120.00"


def test_the_writer_refuses_a_field_that_no_longer_holds_the_value_shown(make_product, colleague):
    product = make_product(price="100.00")
    inventory.update_product(product.pk, {"price": "120.00"}, reason="admin edit", staff=colleague)

    with pytest.raises(inventory.ProductConflict) as refused:
        inventory.update_product(
            product.pk, {"price": "150.00"}, reason="admin edit", seen={"price": "100.00"}
        )

    assert [(c.field, str(c.current), c.author) for c in refused.value.conflicts] == [
        ("price", "120.00", "kitchen_lead")
    ]
    product.refresh_from_db()
    assert str(product.price) == "120.00"


@pytest.mark.django_db(transaction=True)
def test_the_migration_gives_earlier_stock_rows_their_source():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = ("menu", "0003_product_version"), ("menu", "0004_change_authors")
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        MigrationExecutor(connection).migrate([before])
        apps = MigrationExecutor(connection).loader.project_state([before]).apps
        # the accounts table is at its latest schema whatever menu is at, so its own model writes it
        staff = get_user_model().objects.create_user(username="kitchen_lead", password="x").pk
        product = apps.get_model("menu", "Product").objects.create(
            code="m_1", category="dish", name="Рамен", price="1.00", stock_quantity=1
        )
        rows = apps.get_model("menu", "StockAdjustment").objects
        reasons = {
            "order #4": ("sale", None),
            "cancelled order #4": ("cancellation", staff),
            "поставка": ("staff", staff),
            "initial stock": ("system", None),
        }
        ids = {
            reason: rows.create(
                product=product, staff_id=who, old_quantity=1, new_quantity=1, reason=reason
            ).pk
            for reason, (_, who) in reasons.items()
        }

        MigrationExecutor(connection).migrate([after])

        migrated = MigrationExecutor(connection).loader.project_state([after]).apps
        stored = migrated.get_model("menu", "StockAdjustment").objects
        assert {reason: stored.get(pk=pk).source for reason, pk in ids.items()} == {
            reason: source for reason, (source, _) in reasons.items()
        }
    finally:
        MigrationExecutor(connection).migrate(leaves)

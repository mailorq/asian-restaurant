import threading
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from cart import service as cart_service
from menu import inventory
from menu.management.commands import seed_menu
from menu.management.commands.seed_menu import _load_products
from menu.models import Product, StockAdjustment
from orders import service as order_service
from orders.models import OrderOutbox
from tests.concurrency import Writer, queue_behind

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path, monkeypatch):
    # the real images weigh megabytes and /tmp is a small tmpfs; the command only copies them
    source = tmp_path / "source"
    source.mkdir()
    for row in _load_products():
        (source / row["image"].rsplit("/", 1)[-1]).write_bytes(b"image")
    monkeypatch.setattr(seed_menu, "SOURCE_IMAGES", source)
    settings.MEDIA_ROOT = tmp_path / "media"
    return tmp_path / "media"


def _state():
    return (
        list(
            Product.objects.order_by("code").values_list(
                "code", "name", "price", "is_active", "is_featured", "stock_quantity", "version"
            )
        ),
        StockAdjustment.objects.count(),
        OrderOutbox.objects.count(),
    )


def test_a_first_seed_creates_the_catalog_without_stock():
    call_command("seed_menu")

    products = Product.objects.all()
    assert products.count() == len(_load_products())
    assert set(products.values_list("stock_quantity", flat=True)) == {0}
    assert not StockAdjustment.objects.exists()
    assert OrderOutbox.objects.filter(event_type=inventory.STOCK_EVENT).count() == products.count()


def test_a_repeated_seed_changes_nothing_that_operations_did():
    call_command("seed_menu")
    product = Product.objects.get(code="dish_1")
    inventory.update_product(
        product.id, {"stock_quantity": 7, "name": "Рамен сезонный"}, reason="продажи"
    )
    Product.objects.filter(pk=product.pk).update(price=Decimal("999.00"), is_active=False)
    before = _state()

    call_command("seed_menu")

    assert _state() == before


def test_a_seed_adds_only_the_missing_products():
    call_command("seed_menu")
    Product.objects.filter(code="drink_1").delete()
    before = OrderOutbox.objects.count()

    call_command("seed_menu")

    assert Product.objects.filter(code="drink_1", stock_quantity=0).exists()
    assert OrderOutbox.objects.count() == before + 1


def test_a_seed_copies_missing_images_and_keeps_existing_ones(_media):
    (_media / "products").mkdir(parents=True)
    replaced = _media / "products" / "dish_1.webp"
    replaced.write_bytes(b"uploaded by an admin")

    call_command("seed_menu")

    assert replaced.read_bytes() == b"uploaded by an admin"
    assert (_media / "products" / "dish_2.webp").stat().st_size > 0


def test_initial_stock_needs_an_explicit_confirmation():
    call_command("seed_menu")

    with pytest.raises(CommandError):
        call_command("seed_initial_stock")

    assert set(Product.objects.values_list("stock_quantity", flat=True)) == {0}


def test_initial_stock_goes_through_the_stock_writer_once():
    call_command("seed_menu")

    call_command("seed_initial_stock", "--confirm", "--quantity", "40")

    assert set(Product.objects.values_list("stock_quantity", flat=True)) == {40}
    assert set(Product.objects.values_list("version", flat=True)) == {2}
    assert StockAdjustment.objects.filter(reason="initial stock").count() == Product.objects.count()
    with pytest.raises(CommandError):
        call_command("seed_initial_stock", "--confirm")
    assert set(Product.objects.values_list("stock_quantity", flat=True)) == {40}


def test_initial_stock_is_refused_once_the_shop_has_orders(user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1})
    order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-seed-refused")
    before = _state()

    with pytest.raises(CommandError):
        call_command("seed_initial_stock", "--confirm")

    assert _state() == before


def _stocked_product(code):
    return Product.objects.create(
        code=code,
        category="dish",
        name="Рамен",
        description="d",
        price="100.00",
        stock_quantity=10,
        is_active=True,
    )


def _buy(product, quantity, n):
    buyer = get_user_model().objects.create_user(
        username=f"+7999100{n:04d}", password="Pass!2345", phone=f"+7999100{n:04d}"
    )
    cart_service._sync_redis().hset(
        cart_service.user_key(buyer.id),
        mapping={str(product.id): quantity, cart_service.VERSION_FIELD: 1},
    )
    order_service.checkout(buyer, "ул. Пушкина, 12", "cash", f"idem-seed-race-{n}")


def _seed(outcome):
    def run():
        try:
            call_command("seed_initial_stock", "--confirm", "--quantity", "50")
            outcome["seed"] = "applied"
        except CommandError:
            outcome["seed"] = "refused"

    return run


@pytest.mark.django_db(transaction=True)
def test_initial_stock_queued_behind_a_sale_is_refused_after_it(monkeypatch):
    product = _stocked_product("race_seed_sale_first")
    holding, release, outcome = threading.Event(), threading.Event(), {}
    real = inventory.record_stock_change

    def sale_holds_the_row(*args, **kwargs):
        real(*args, **kwargs)
        if threading.current_thread().name == "sale":
            holding.set()
            assert release.wait(timeout=20)

    monkeypatch.setattr(inventory, "record_stock_change", sale_holds_the_row)
    sale = Writer("sale", lambda: _buy(product, 3, 1))
    seed = Writer("seed", _seed(outcome))
    met = queue_behind(sale, seed, holding, release)

    assert (sale.error, seed.error) == (None, None)
    assert outcome == {"seed": "refused"}
    product.refresh_from_db()
    assert product.stock_quantity == 7
    assert met == "blocked"


@pytest.mark.django_db(transaction=True)
def test_a_sale_queued_behind_initial_stock_is_taken_from_it(monkeypatch):
    product = _stocked_product("race_seed_first")
    holding, release, outcome = threading.Event(), threading.Event(), {}
    real = inventory.set_stock

    def seed_holds_the_catalog(*args, **kwargs):
        if threading.current_thread().name == "seed" and not holding.is_set():
            holding.set()
            assert release.wait(timeout=20)
        return real(*args, **kwargs)

    monkeypatch.setattr(inventory, "set_stock", seed_holds_the_catalog)
    seed = Writer("seed", _seed(outcome))
    sale = Writer("sale", lambda: _buy(product, 3, 2))
    met = queue_behind(seed, sale, holding, release)

    assert (seed.error, sale.error) == (None, None)
    assert outcome == {"seed": "applied"}
    product.refresh_from_db()
    assert product.stock_quantity == 47
    assert met == "blocked"

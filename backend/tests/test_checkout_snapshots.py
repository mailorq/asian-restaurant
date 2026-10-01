import threading

import pytest
from asgiref.sync import async_to_sync
from django.db import connection

from cart import service as cart_service
from orders import service as order_service
from orders.models import Order
from orders.service import CheckoutError
from tests.concurrency import WAIT_SECONDS, Writer

ADDRESS = "ул. Пушкина, 12"


def _run(coro_func, *args):
    cart_service._clients.clear()
    return async_to_sync(coro_func)(*args)


def _bought(order):
    return {item.product_id: item.quantity for item in order.items.all()}


class _Geocoding:
    """holds the named checkout in its geocoding: it has read the cart and has not opened its transaction"""

    def __init__(self, monkeypatch, name):
        self.read = threading.Event()
        self.resume = threading.Event()
        geocode = order_service._geocode_safe

        def held(address):
            if threading.current_thread().name == name:
                self.read.set()
                assert self.resume.wait(WAIT_SECONDS), f"{name} was never resumed"
            return geocode(address)

        monkeypatch.setattr(order_service, "_geocode_safe", held)


def _checkout_in(name, user, results):
    return Writer(name, lambda: results.update({name: order_service.checkout(user, ADDRESS, "cash", name)}))


@pytest.mark.django_db(transaction=True)
def test_a_snapshot_read_before_another_order_of_the_cart_committed_is_turned_back(
    user, make_product, seed_cart, monkeypatch
):
    bought = make_product(stock=10)
    added = make_product(stock=10)
    seed_cart(user.id, {bought.id: 2}, version=1)
    key = cart_service.user_key(user.id)
    first = _Geocoding(monkeypatch, "first-form")
    results: dict = {}
    connection.close()
    stale = _checkout_in("first-form", user, results)
    stale.start()
    try:
        assert first.read.wait(WAIT_SECONDS), "the first checkout never read the cart"
        _run(cart_service.add, key, added.id, 1, None)
        placed = order_service.checkout(user, ADDRESS, "cash", "second-form")
    finally:
        first.resume.set()
        stale.join(WAIT_SECONDS)

    assert isinstance(stale.error, CheckoutError) and stale.error.code == "cart_changed"
    assert list(Order.objects.all()) == [placed]
    assert _bought(placed) == {bought.id: 2, added.id: 1}
    bought.refresh_from_db()
    assert bought.stock_quantity == 8
    assert cart_service.read_sync(key).items == {}


@pytest.mark.django_db(transaction=True)
def test_a_snapshot_read_before_the_cart_was_bought_from_is_turned_back_and_the_new_line_stays(
    user, make_product, seed_cart, monkeypatch
):
    bought = make_product(stock=10)
    added = make_product(stock=10)
    seed_cart(user.id, {bought.id: 2}, version=1)
    key = cart_service.user_key(user.id)
    first = _Geocoding(monkeypatch, "first-form")
    second = _Geocoding(monkeypatch, "second-form")
    results: dict = {}
    connection.close()
    earlier, later = _checkout_in("first-form", user, results), _checkout_in("second-form", user, results)
    earlier.start()
    try:
        assert first.read.wait(WAIT_SECONDS), "the first checkout never read the cart"
        _run(cart_service.add, key, added.id, 1, None)
        later.start()
        assert second.read.wait(WAIT_SECONDS), "the second checkout never read the cart"
        first.resume.set()
        earlier.join(WAIT_SECONDS)
    finally:
        first.resume.set()
        second.resume.set()
        earlier.join(WAIT_SECONDS)
        later.join(WAIT_SECONDS)

    assert earlier.error is None
    assert isinstance(later.error, CheckoutError) and later.error.code == "cart_changed"
    assert _bought(results["first-form"]) == {bought.id: 2}
    assert cart_service.read_sync(key).items == {added.id: 1}
    resubmitted = order_service.checkout(user, ADDRESS, "cash", "third-form")
    assert _bought(resubmitted) == {added.id: 1}
    bought.refresh_from_db()
    assert bought.stock_quantity == 8

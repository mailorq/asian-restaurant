"""the cart version must never be reused

optimistic locking is the whole contract of the cart API, and it only holds while the version
moves in one direction. emptying the cart deleted the key, which reset the counter to zero, so a
write addressed to a previous cart generation could pass a check meant to reject it
"""

import pytest
from asgiref.sync import async_to_sync

from cart import service

pytestmark = pytest.mark.django_db

KEY = "cart:u:9001"


@pytest.fixture(autouse=True)
def _clean():
    service._sync_redis().delete(KEY)
    yield
    service._sync_redis().delete(KEY)


def _run(coro_func, *args):
    service._clients.clear()
    return async_to_sync(coro_func)(*args)


def test_clearing_the_cart_does_not_rewind_the_version(make_product):
    product = make_product(stock=10)
    before = _run(service.add, KEY, product.id, 1, None)

    after = _run(service.clear, KEY, before)

    assert after > before, "a cleared cart must not hand back a version it already used"


def test_a_write_from_the_previous_cart_generation_is_refused(make_product):
    product = make_product(stock=10)
    stale = _run(service.add, KEY, product.id, 1, None)
    _run(service.clear, KEY, stale)
    _run(service.add, KEY, product.id, 5, None)

    with pytest.raises(service.CartConflict):
        _run(service.set_qty, KEY, product.id, 99, stale)


def test_checkout_emptying_the_cart_does_not_rewind_the_version(make_product):
    product = make_product(stock=10)
    version = _run(service.add, KEY, product.id, 2, None)

    left = service.remove_purchased_sync(KEY, {product.id: 2})

    assert left > version, "the cart is emptied on every checkout, so this rewind is routine"


def test_a_write_addressed_to_the_bought_cart_is_refused(make_product):
    product = make_product(stock=10)
    bought_at = _run(service.add, KEY, product.id, 2, None)
    service.remove_purchased_sync(KEY, {product.id: 2})
    _run(service.add, KEY, product.id, 1, None)

    with pytest.raises(service.CartConflict):
        _run(service.add, KEY, product.id, 7, bought_at)


def test_the_version_survives_a_version_scoped_clear(make_product):
    product = make_product(stock=10)
    version = _run(service.add, KEY, product.id, 1, None)

    assert service.clear_sync(KEY, version, service.read_sync(KEY).cart_id) is True

    _, current = _run(service.read, KEY)
    assert current > version


def test_an_emptied_cart_still_reads_as_empty(make_product):
    product = make_product(stock=10)
    version = _run(service.add, KEY, product.id, 1, None)
    _run(service.clear, KEY, version)

    items, current = _run(service.read, KEY)

    assert items == {}
    assert current > version


def test_a_cart_that_never_existed_still_starts_at_zero():
    items, version = _run(service.read, "cart:u:9002")

    assert (items, version) == ({}, 0)

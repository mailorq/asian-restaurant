import pytest
from asgiref.sync import async_to_sync

from cart import service as cart_service
from orders import service as order_service
from orders.models import Order

pytestmark = pytest.mark.django_db

ADDRESS = "ул. Пушкина, 12"


def _run(coro_func, *args):
    cart_service._clients.clear()
    return async_to_sync(coro_func)(*args)


def _expire(key: str) -> None:
    cart_service._sync_redis().delete(key)


def test_a_cart_rebuilt_after_expiry_is_ordered_as_a_new_cart(user, make_product):
    product = make_product(price="100.00", stock=10)
    key = cart_service.user_key(user.id)
    assert _run(cart_service.add, key, product.id, 1, None) == 1
    first = order_service.checkout(user, ADDRESS, "cash", "idem-generation-1")

    _expire(key)
    assert _run(cart_service.add, key, product.id, 1, None) == 1
    second = order_service.checkout(user, ADDRESS, "cash", "idem-generation-2")

    assert second.pk != first.pk
    assert Order.objects.filter(user=user).count() == 2
    product.refresh_from_db()
    assert product.stock_quantity == 8


def test_a_rebuilt_cart_with_other_items_is_not_answered_with_the_old_order(user, make_product):
    old = make_product(price="100.00", stock=10)
    new = make_product(price="300.00", stock=10)
    key = cart_service.user_key(user.id)
    _run(cart_service.add, key, old.id, 1, None)
    order_service.checkout(user, ADDRESS, "cash", "idem-other-items-1")

    _expire(key)
    _run(cart_service.add, key, new.id, 2, None)
    order = order_service.checkout(user, ADDRESS, "cash", "idem-other-items-2")

    assert [(item.product_id, item.quantity) for item in order.items.all()] == [(new.id, 2)]
    assert cart_service.read_sync(key).items == {}


def test_retrying_an_old_order_leaves_a_rebuilt_cart_alone(user, make_product):
    product = make_product(price="100.00", stock=10)
    key = cart_service.user_key(user.id)
    _run(cart_service.add, key, product.id, 1, None)
    first = order_service.checkout(user, ADDRESS, "cash", "idem-retry-1")

    _expire(key)
    _run(cart_service.add, key, product.id, 1, None)
    again = order_service.checkout(user, ADDRESS, "cash", "idem-retry-1")

    assert again.pk == first.pk
    assert cart_service.read_sync(key).items == {product.id: 1}


def test_the_identity_survives_writes_and_clearing_and_is_new_after_expiry(make_product):
    product = make_product(stock=10)
    key = cart_service.user_key(9101)
    _expire(key)
    _run(cart_service.add, key, product.id, 2, None)
    identity = cart_service.read_sync(key).cart_id
    assert identity

    version = _run(cart_service.clear, key, None)
    assert cart_service.read_sync(key).cart_id == identity
    _run(cart_service.add, key, product.id, 2, version)
    cart_service.remove_purchased_sync(key, {product.id: 1})
    assert cart_service.read_sync(key).cart_id == identity
    state = cart_service.read_sync(key)
    assert cart_service.clear_sync(key, state.version, state.cart_id) is True
    assert cart_service.read_sync(key).cart_id == identity

    _expire(key)
    _run(cart_service.add, key, product.id, 1, None)
    assert cart_service.read_sync(key).cart_id not in ("", identity)
    _expire(key)


def test_a_clear_addressed_to_an_expired_cart_leaves_the_new_one_alone(make_product):
    product = make_product(stock=10)
    key = cart_service.user_key(9102)
    _expire(key)
    _run(cart_service.add, key, product.id, 1, None)
    stale = cart_service.read_sync(key)

    _expire(key)
    _run(cart_service.add, key, product.id, 1, None)

    assert cart_service.clear_sync(key, stale.version, stale.cart_id) is False
    assert cart_service.read_sync(key).items == {product.id: 1}
    _expire(key)


def test_a_merge_keeps_the_user_identity_and_moves_only_items(make_product):
    product = make_product(stock=10)
    user_key = cart_service.user_key(9103)
    guest_key = cart_service.guest_key("identity-merge")
    _expire(user_key)
    _run(cart_service.add, user_key, product.id, 1, None)
    _run(cart_service.add, guest_key, product.id, 2, None)
    identity = cart_service.read_sync(user_key).cart_id

    _run(cart_service.merge_guest_into_user, guest_key, user_key)

    state = cart_service.read_sync(user_key)
    assert state.items == {product.id: 3}
    assert state.cart_id == identity
    _expire(user_key)


def test_a_cart_stored_without_an_identity_gets_one_that_then_stays(make_product):
    product = make_product(stock=10)
    key = cart_service.user_key(9104)
    cart_service._sync_redis().hset(
        key, mapping={str(product.id): 1, cart_service.VERSION_FIELD: 4}
    )

    first = cart_service.read_sync(key)
    second = cart_service.read_sync(key)

    assert first.cart_id and first.cart_id == second.cart_id
    assert (first.items, first.version) == ({product.id: 1}, 4)
    _expire(key)

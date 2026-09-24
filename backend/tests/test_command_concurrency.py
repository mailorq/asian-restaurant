import datetime as dt
import threading
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from event_contracts import OrderTransitionRequestedData

from accounts.roles import StaffRole
from cart import service as cart_service
from menu.models import Product
from orders import commands
from orders import service as order_service
from orders.models import CommandInbox, OrderOutbox, OrderStatusHistory
from tests.concurrency import Writer, queue_behind


def _actor():
    account = get_user_model().objects.create_user(
        username="+79990000777", password="Pass!2345", phone="+79990000777"
    )
    account.groups.add(Group.objects.get_or_create(name=StaffRole.MANAGER)[0])
    account.authz_version += 1
    account.save(update_fields=["authz_version"])
    account.refresh_from_db()
    return account


def _order(actor):
    product = Product.objects.create(
        code="conc_1", category="dish", name="Рамен", description="d",
        price="100.00", stock_quantity=10, is_active=True,
    )
    key = cart_service.user_key(actor.id)
    cart_service._sync_redis().hset(
        key, mapping={str(product.id): 2, cart_service.VERSION_FIELD: 1}
    )
    return order_service.checkout(actor, "ул. Пушкина, 12", "cash", "idem-conc-1")


@pytest.mark.django_db(transaction=True)
def test_two_deliveries_of_one_command_apply_it_once(monkeypatch):
    actor = _actor()
    order = _order(actor)
    data = OrderTransitionRequestedData(
        command_id=uuid.uuid4(), actor_id=actor.id, actor_authz_version=actor.authz_version,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60),
        order_id=order.id, expected_status="created", target_status="confirmed",
    )
    request_event_id, correlation_id = uuid.uuid4(), uuid.uuid4()
    holding, release, results = threading.Event(), threading.Event(), {}
    real = commands._actor

    def first_holds_its_claim(requested):
        found = real(requested)
        if threading.current_thread().name == "first":
            holding.set()
            assert release.wait(timeout=20)
        return found

    monkeypatch.setattr(commands, "_actor", first_holds_its_claim)

    def deliver(name):
        def run():
            results[name] = commands.apply_transition_command(
                data, request_event_id=request_event_id, correlation_id=correlation_id
            )

        return run

    first, second = Writer("first", deliver("first")), Writer("second", deliver("second"))
    met = queue_behind(first, second, holding, release)

    assert (first.error, second.error) == (None, None)
    assert (results["first"].replayed, results["second"].replayed) == (False, True)
    assert results["first"].data == results["second"].data
    order.refresh_from_db()
    assert order.status == "confirmed"
    assert CommandInbox.objects.filter(command_id=data.command_id).count() == 1
    assert OrderOutbox.objects.filter(event_type="orders.transition.succeeded.v1").count() == 1
    assert OrderStatusHistory.objects.filter(order=order, to_status="confirmed").count() == 1
    assert met == "blocked"
    assert second.blocked_in.lower().startswith('insert into "orders_commandinbox"')

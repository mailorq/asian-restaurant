import datetime as dt
import threading
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from event_contracts import OrderTransitionRequestedData

from accounts.roles import StaffRole
from cart import service as cart_service
from menu.models import Product
from orders import commands
from orders import service as order_service
from orders.models import CommandInbox, Order, OrderStatusHistory
from tests.concurrency import Writer, queue_behind


def _employee(phone):
    account = get_user_model().objects.create_user(
        username=phone, password="Pass!2345", phone=phone
    )
    account.groups.add(Group.objects.get_or_create(name=StaffRole.MANAGER)[0])
    # a role change bumps the authorization version, and the command carries the current one
    account.refresh_from_db()
    return account


def _order():
    customer = get_user_model().objects.create_user(
        username="+79990000900", password="Pass!2345", phone="+79990000900"
    )
    product = Product.objects.create(
        code="mixed_1", category="dish", name="Рамен", price="100.00", stock_quantity=10
    )
    cart_service._sync_redis().hset(
        cart_service.user_key(customer.id),
        mapping={str(product.id): 1, cart_service.VERSION_FIELD: 1},
    )
    return order_service.checkout(customer, "ул. Пушкина, 12", "cash", "idem-mixed-1")


def _command(actor, order):
    return OrderTransitionRequestedData(
        command_id=uuid.uuid4(),
        actor_id=actor.id,
        actor_authz_version=actor.authz_version,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60),
        order_id=order.id,
        expected_status="created",
        target_status="confirmed",
    )


def _apply(data):
    return commands.apply_transition_command(
        data, request_event_id=uuid.uuid4(), correlation_id=uuid.uuid4()
    )


@pytest.mark.django_db(transaction=True)
def test_a_command_meeting_a_direct_write_it_lost_to_is_rejected_as_stale():
    switched, direct = _employee("+79990000901"), _employee("+79990000902")
    order = _order()
    data = _command(switched, order)
    holding, release, results = threading.Event(), threading.Event(), {}

    def direct_write():
        with transaction.atomic():
            order_service.transition(
                order, "cancelled", changed_by=direct, expected_status="created"
            )
            holding.set()
            assert release.wait(timeout=20)

    def command():
        results["command"] = _apply(data)

    writer, consumer = Writer("direct", direct_write), Writer("command", command)
    met = queue_behind(writer, consumer, holding, release)

    assert (writer.error, consumer.error, met) == (None, None, "blocked")
    assert consumer.blocked_in.lower().startswith('select "orders_order"')
    order.refresh_from_db()
    assert order.status == "cancelled"
    outcome = CommandInbox.objects.get(command_id=data.command_id).outcome_data
    assert (outcome["reject_code"], outcome["current_status"]) == ("stale_status", "cancelled")
    assert OrderStatusHistory.objects.filter(order=order).exclude(to_status="created").count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_direct_write_meeting_a_command_it_lost_to_is_refused_as_stale(monkeypatch):
    switched, direct = _employee("+79990000901"), _employee("+79990000902")
    order = _order()
    data = _command(switched, order)
    holding, release, results = threading.Event(), threading.Event(), {}
    real = commands.order_service.transition

    def command_holds_the_order(*args, **kwargs):
        applied = real(*args, **kwargs)
        if threading.current_thread().name == "command":
            holding.set()
            assert release.wait(timeout=20)
        return applied

    monkeypatch.setattr(commands.order_service, "transition", command_holds_the_order)

    def command():
        results["command"] = _apply(data)

    def direct_write():
        with pytest.raises(order_service.CheckoutError) as refused, transaction.atomic():
            real(
                Order.objects.get(pk=order.pk),
                "cancelled",
                changed_by=direct,
                expected_status="created",
            )
        results["direct"] = refused.value.code

    consumer, writer = Writer("command", command), Writer("direct", direct_write)
    met = queue_behind(consumer, writer, holding, release)

    assert (consumer.error, writer.error, met) == (None, None, "blocked")
    assert writer.blocked_in.lower().startswith('select "orders_order"')
    order.refresh_from_db()
    assert (order.status, results["direct"], results["command"].replayed) == (
        "confirmed",
        "stale_order",
        False,
    )
    assert OrderStatusHistory.objects.filter(order=order).exclude(to_status="created").count() == 1

import datetime as dt
import uuid

import pytest
from django.contrib.auth.models import Group
from event_contracts import OrderTransitionRequestedData

from accounts.roles import StaffRole
from orders import commands
from orders.models import CommandInbox, Order, OrderOutbox, OrderStatusHistory

pytestmark = pytest.mark.django_db


def _request(actor, order_id, *, expected="created", target="confirmed", ttl_seconds=30,
             authz_version=None, command_id=None, reason="") -> OrderTransitionRequestedData:
    return OrderTransitionRequestedData(
        command_id=command_id or uuid.uuid4(),
        actor_id=actor.id,
        actor_authz_version=authz_version if authz_version is not None else actor.authz_version,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds),
        order_id=order_id,
        expected_status=expected,
        target_status=target,
        reason=reason,
    )


def _apply(data):
    return commands.apply_transition_command(
        data, request_event_id=uuid.uuid4(), correlation_id=uuid.uuid4()
    )


@pytest.fixture
def order(user, make_product, seed_cart):
    from orders import service as order_service

    product = make_product(price="100.00", stock=10)
    seed_cart(user.id, {product.id: 2}, version=1)
    return order_service.checkout(user, "ул. Пушкина, 12", "cash", f"idem-{uuid.uuid4().hex[:8]}")


def _grant(account):

    account.groups.add(Group.objects.get_or_create(name=StaffRole.MANAGER)[0])
    account.authz_version += 1
    account.save(update_fields=["authz_version"])
    account.refresh_from_db()
    return account


def test_success_applies_transition_and_records_outcome(order, employee_user):
    actor = _grant(employee_user)
    outcome = _apply(_request(actor, order.id))

    assert outcome.event_type == "orders.transition.succeeded.v1"
    assert outcome.data["from_status"] == "created" and outcome.data["status"] == "confirmed"
    order.refresh_from_db()
    assert order.status == "confirmed"

    inbox = CommandInbox.objects.get()
    row = OrderOutbox.objects.get(event_type="orders.transition.succeeded.v1")
    assert row.event_id == inbox.outcome_event_id
    assert row.causation_id == inbox.request_event_id
    assert row.correlation_id == inbox.correlation_id
    assert row.routing_key == "order.transition_succeeded"


def test_duplicate_command_replays_stored_outcome(order, employee_user):
    actor = _grant(employee_user)
    data = _request(actor, order.id)
    first = _apply(data)
    history = OrderStatusHistory.objects.filter(order=order).count()

    second = _apply(data)

    assert second.replayed is True
    assert second.data == first.data
    assert CommandInbox.objects.count() == 1
    assert OrderOutbox.objects.filter(event_type="orders.transition.succeeded.v1").count() == 1
    assert OrderStatusHistory.objects.filter(order=order).count() == history


def test_replay_after_expiry_still_returns_stored_outcome(order, employee_user):
    actor = _grant(employee_user)
    data = _request(actor, order.id, ttl_seconds=30)
    first = _apply(data)

    expired = _request(actor, order.id, ttl_seconds=-30, command_id=data.command_id)
    again = _apply(expired)

    assert again.replayed is True and again.data == first.data


def test_expired_command_is_rejected_without_touching_the_order(order, employee_user):
    actor = _grant(employee_user)
    outcome = _apply(_request(actor, order.id, ttl_seconds=-1))

    assert outcome.reject_code == "command_expired"
    assert outcome.data["current_status"] is None
    order.refresh_from_db()
    assert order.status == "created"


def test_actor_revoked_after_creation_is_rejected(order, employee_user):

    actor = _grant(employee_user)
    data = _request(actor, order.id)
    actor.groups.clear()
    actor.authz_version += 1
    actor.save(update_fields=["authz_version"])

    outcome = _apply(data)

    assert outcome.reject_code == "actor_not_authorized"
    order.refresh_from_db()
    assert order.status == "created"


def test_deactivated_actor_is_rejected(order, employee_user):

    actor = _grant(employee_user)
    data = _request(actor, order.id)
    actor.is_active = False
    actor.authz_version += 1
    actor.save(update_fields=["is_active", "authz_version"])

    outcome = _apply(data)

    assert outcome.reject_code == "actor_not_authorized"


def test_stale_authz_version_is_rejected(order, employee_user):
    actor = _grant(employee_user)
    outcome = _apply(_request(actor, order.id, authz_version=actor.authz_version + 5))

    assert outcome.reject_code == "actor_not_authorized"


def test_customer_actor_is_rejected(order, user):
    outcome = _apply(_request(user, order.id, authz_version=max(1, user.authz_version)))

    assert outcome.reject_code == "actor_not_authorized"


def test_unknown_order_is_rejected(employee_user):
    actor = _grant(employee_user)
    outcome = _apply(_request(actor, 999999))

    assert outcome.reject_code == "order_not_found"
    assert outcome.data["current_status"] is None


def test_stale_status_is_rejected_with_current_status(order, employee_user):
    actor = _grant(employee_user)
    outcome = _apply(_request(actor, order.id, expected="preparing", target="delivering"))

    assert outcome.reject_code == "stale_status"
    assert outcome.data["current_status"] == "created"
    order.refresh_from_db()
    assert order.status == "created"


def test_invalid_transition_is_rejected_with_current_status(order, employee_user):
    actor = _grant(employee_user)
    outcome = _apply(_request(actor, order.id, expected="created", target="delivered"))

    assert outcome.reject_code == "invalid_transition"
    assert outcome.data["current_status"] == "created"


def test_cancel_command_restores_stock(order, employee_user, make_product):
    actor = _grant(employee_user)
    product = order.items.first().product
    before = product.stock_quantity

    outcome = _apply(_request(actor, order.id, expected="created", target="cancelled"))

    assert outcome.event_type == "orders.transition.succeeded.v1"
    product.refresh_from_db()
    assert product.stock_quantity == before + order.items.first().quantity


def test_rejected_outcome_is_also_recorded_in_the_inbox(order, employee_user):
    actor = _grant(employee_user)
    data = _request(actor, order.id, ttl_seconds=-1)
    _apply(data)

    inbox = CommandInbox.objects.get(command_id=data.command_id)
    row = OrderOutbox.objects.get(event_type="orders.transition.rejected.v1")
    assert inbox.outcome_type == "orders.transition.rejected.v1"
    assert row.event_id == inbox.outcome_event_id
    assert Order.objects.get(pk=order.id).status == "created"


def test_relay_headers_carry_causation_from_the_outcome_row(order, employee_user):
    from orders.management.commands.publish_outbox import _headers

    actor = _grant(employee_user)
    _apply(_request(actor, order.id))
    row = OrderOutbox.objects.get(event_type="orders.transition.succeeded.v1")

    headers = _headers(row)

    assert headers["causation_id"] == str(row.causation_id)
    assert headers["correlation_id"] == str(row.correlation_id)
    assert headers["event_id"] == str(row.event_id)


def test_relay_headers_have_no_causation_for_plain_events(order):
    from orders.management.commands.publish_outbox import _headers

    row = OrderOutbox.objects.filter(event_type="order.created").first()

    assert _headers(row)["causation_id"] is None


def test_a_staff_role_without_the_orders_capability_cannot_transition(order, employee_user, monkeypatch):
    # the command plane must require the capability the http endpoint requires, or one path grants what the other refuses
    from accounts import roles

    actor = _grant(employee_user)
    monkeypatch.setitem(roles.CAPABILITIES, StaffRole.MANAGER, frozenset({roles.Capability.INVENTORY}))

    outcome = _apply(_request(actor, order.id))

    assert outcome.data.get("reject_code") == "actor_not_authorized", outcome.data
    order.refresh_from_db()
    assert order.status == "created"

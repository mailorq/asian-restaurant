import json
import uuid

import httpx
import jwt as pyjwt
import pytest
from django.test import Client

from cart import service as cart_service
from employee import command_plane
from menu.models import Product
from orders import service as order_service
from orders.models import OrderStatusHistory

pytestmark = pytest.mark.django_db

INTENT = {"expected_status": "created", "target_status": "confirmed", "reason": "kitchen"}


def _command(status="pending"):
    return {
        "command_id": str(uuid.uuid4()),
        "status": status,
        "result_code": "",
        "result_detail": "",
        "deadline_at": None,
        "created_at": "2026-09-26T10:00:00Z",
    }


class Operations:
    """stands in for the operations api: keeps what reached it and answers what the test set"""

    def __init__(self):
        self.seen = []
        self.answers = []

    def __call__(self, request):
        self.seen.append(request)
        answer = self.answers.pop(0) if self.answers else httpx.Response(202, json=_command())
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def operations(monkeypatch):
    stand_in = Operations()
    client = httpx.Client(
        base_url="http://operations-api:9000", transport=httpx.MockTransport(stand_in)
    )
    monkeypatch.setattr(command_plane, "_client", lambda: client)
    return stand_in


@pytest.fixture
def switched(employee_user):
    employee_user.transitions_via_commands = True
    employee_user.save(update_fields=["transitions_via_commands"])
    return employee_user


@pytest.fixture
def panel(switched):
    client = Client()
    client.force_login(switched)
    return client


@pytest.fixture
def order(user):
    product = Product.objects.create(
        code="bff_1", category="dish", name="Рамен", price="100.00", stock_quantity=10
    )
    cart_service._sync_redis().hset(
        cart_service.user_key(user.id), mapping={str(product.id): 1, cart_service.VERSION_FIELD: 1}
    )
    return order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-bff-1")


def _post(client, order_id=5, key="k1", body=INTENT):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        f"/api/employee/orders/{order_id}/transition-commands",
        data=json.dumps(body),
        content_type="application/json",
        headers=headers,
    )


def test_the_key_and_the_intent_reach_operations_as_sent_with_a_token_minted_here(
    panel, switched, operations
):
    response = _post(panel, key="  k1  ")

    assert response.status_code == 202
    (sent,) = operations.seen
    assert (sent.method, sent.url.path) == ("POST", "/ops-api/orders/5/transition-commands")
    assert sent.headers["Idempotency-Key"] == "k1"
    assert json.loads(sent.content) == INTENT
    token = sent.headers["Authorization"].removeprefix("Bearer ")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert (claims["sub"], claims["authz_version"]) == (str(switched.id), switched.authz_version)
    body = response.json()
    assert response["Location"] == f"/api/employee/commands/{body['command_id']}"
    assert token not in response.content.decode() and token not in str(response.headers)


@pytest.mark.parametrize("key", [None, "   ", "x" * 201, "a\x00b"])
def test_a_key_operations_would_refuse_is_refused_before_a_token_is_minted(panel, operations, key):
    response = _post(panel, key=key)

    assert response.status_code == 422
    assert operations.seen == []


def test_every_request_is_forwarded_so_operations_alone_decides_what_a_repeat_is(panel, operations):
    operations.answers = [
        httpx.Response(202, json=_command()),
        httpx.Response(200, json=_command()),
    ]

    first = _post(panel, order_id=5, key="k1")
    other_order = _post(panel, order_id=6, key="k1")

    assert (first.status_code, other_order.status_code) == (202, 200)
    assert [r.url.path for r in operations.seen] == [
        "/ops-api/orders/5/transition-commands",
        "/ops-api/orders/6/transition-commands",
    ]
    assert {r.headers["Idempotency-Key"] for r in operations.seen} == {"k1"}


@pytest.mark.parametrize("status", [429, 503])
def test_a_refusal_of_operations_reaches_the_browser_with_its_wait(panel, operations, status):
    operations.answers = [
        httpx.Response(status, headers={"Retry-After": "17"}, json={"detail": "x"})
    ]

    response = _post(panel)

    assert (response.status_code, response["Retry-After"]) == (status, "17")


@pytest.mark.parametrize(("answer", "status"), [(409, 409), (422, 422), (401, 403), (500, 502)])
def test_other_answers_of_operations_are_translated(panel, operations, answer, status):
    operations.answers = [httpx.Response(answer, json={"detail": "x"})]

    assert _post(panel).status_code == status


def test_a_request_that_never_left_is_a_known_refusal(panel, operations, order):
    operations.answers = [httpx.ConnectError("refused")]

    response = _post(panel, order_id=order.id)

    assert (response.status_code, response.json()["code"], response["Retry-After"]) == (
        503,
        "not_sent",
        "1",
    )
    order.refresh_from_db()
    assert order.status == "created"


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")])
def test_a_request_that_may_have_arrived_has_an_unknown_outcome_and_no_other_write_path(
    panel, operations, order, error
):
    operations.answers = [error]

    response = _post(panel, order_id=order.id)

    assert (response.status_code, response.json()["code"]) == (504, "outcome_unknown")
    order.refresh_from_db()
    assert order.status == "created"
    assert list(
        OrderStatusHistory.objects.filter(order=order).values_list("to_status", flat=True)
    ) == ["created"]


def test_an_employee_not_switched_cannot_send_commands(employee_user, operations):
    client = Client()
    client.force_login(employee_user)

    response = _post(client)

    assert (response.status_code, response.json()["code"]) == (403, "transition_mode_changed")
    assert operations.seen == []


def test_a_customer_and_an_anonymous_visitor_cannot_send_commands(user, operations):
    client = Client()
    assert _post(client).status_code == 401
    client.force_login(user)
    assert _post(client).status_code == 403
    assert operations.seen == []


def test_a_command_needs_the_csrf_token(switched, operations):
    client = Client(enforce_csrf_checks=True)
    client.force_login(switched)

    assert _post(client).status_code == 403
    assert operations.seen == []


def test_the_direct_transition_is_closed_to_a_switched_employee(panel, order):
    response = panel.post(
        f"/api/employee/orders/{order.id}/transition",
        data=json.dumps({"to_status": "confirmed", "expected_status": "created"}),
        content_type="application/json",
    )

    assert (response.status_code, response.json()["code"]) == (403, "transition_mode_changed")
    order.refresh_from_db()
    assert order.status == "created"


def test_the_status_of_a_command_is_read_with_a_fresh_token(panel, switched, operations):
    command_id = uuid.uuid4()
    operations.answers = [
        httpx.Response(200, json=_command("succeeded")),
        httpx.Response(404, json={"detail": "not found"}),
    ]

    found = panel.get(f"/api/employee/commands/{command_id}")
    missing = panel.get(f"/api/employee/commands/{uuid.uuid4()}")

    assert (found.status_code, found.json()["status"], missing.status_code) == (
        200,
        "succeeded",
        404,
    )
    assert operations.seen[0].url.path == f"/ops-api/commands/{command_id}"
    token = operations.seen[0].headers["Authorization"].removeprefix("Bearer ")
    assert pyjwt.decode(token, options={"verify_signature": False})["sub"] == str(switched.id)


def test_requests_of_one_employee_are_bounded_repeats_included(panel, operations):
    answers = [_post(panel, key="same").status_code for _ in range(61)]

    assert answers[:60] == [202] * 60
    refused = _post(panel, key="same")
    assert (answers[60], refused["Retry-After"]) == (429, "60")
    assert len(operations.seen) == 60


@pytest.mark.parametrize(
    ("switched_to_commands", "status"), [(True, "created"), (False, "confirmed")]
)
def test_the_admin_transition_is_closed_to_a_switched_superuser_only(
    admin_client, superuser, order, switched_to_commands, status
):
    superuser.transitions_via_commands = switched_to_commands
    superuser.save(update_fields=["transitions_via_commands"])

    admin_client.post(
        "/admin/orders/order/",
        {"action": "transition_to_confirmed", "_selected_action": [order.id], "index": "0"},
    )

    order.refresh_from_db()
    assert order.status == status

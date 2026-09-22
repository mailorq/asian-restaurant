"""
paging and ordering of the order lists

both lists returned every matching row, and the employee one is polled every 15s, so its cost
grows with the whole order history. created_at alone is not unique either, so a page boundary
could repeat or skip a row
"""

import uuid

import pytest
from django.contrib.auth import get_user_model

from orders.models import DeliveryAddress, Order

pytestmark = pytest.mark.django_db

EMPLOYEE_PATH = "/api/employee/orders"
CUSTOMER_PATH = "/api/orders"


def _orders(owner, count, status="created"):
    address = DeliveryAddress.objects.create(user=owner, address="ул. Тестовая, 1")
    return [
        Order.objects.create(
            user=owner, status=status, total="10.00", phone="+79990000001",
            delivery_address=address, idempotency_key=uuid.uuid4().hex,
            source_cart_id=uuid.uuid4().hex, source_cart_version=1,
        )
        for _ in range(count)
    ]


def test_the_employee_list_is_capped_even_when_the_client_asks_for_everything(api, employee_user):
    _orders(employee_user, 30)
    api.force_login(employee_user)

    body = api.get(f"{EMPLOYEE_PATH}?page_size=100000").json()

    assert len(body["items"]) <= 100
    assert body["total"] == 30
    assert body["page_size"] == 100


def test_the_employee_list_pages_without_gaps_or_repeats(api, employee_user):
    _orders(employee_user, 25)
    api.force_login(employee_user)

    seen = []
    for page in (1, 2, 3):
        body = api.get(f"{EMPLOYEE_PATH}?page={page}&page_size=10").json()
        seen.extend(item["id"] for item in body["items"])

    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_the_employee_list_stays_newest_first_across_pages(api, employee_user):
    _orders(employee_user, 12)
    api.force_login(employee_user)

    first = api.get(f"{EMPLOYEE_PATH}?page=1&page_size=6").json()["items"]
    second = api.get(f"{EMPLOYEE_PATH}?page=2&page_size=6").json()["items"]

    ids = [o["id"] for o in first + second]
    assert ids == sorted(ids, reverse=True)


def test_a_nonsense_page_request_is_clamped_not_rejected(api, employee_user):
    _orders(employee_user, 3)
    api.force_login(employee_user)

    body = api.get(f"{EMPLOYEE_PATH}?page=-5&page_size=0").json()

    assert body["page"] == 1
    assert body["page_size"] >= 1
    assert body["items"]


def test_the_status_filter_still_narrows_the_employee_list(api, employee_user):
    _orders(employee_user, 4, status="created")
    _orders(employee_user, 2, status="confirmed")
    api.force_login(employee_user)

    body = api.get(f"{EMPLOYEE_PATH}?status=confirmed").json()

    assert body["total"] == 2
    assert {o["status"] for o in body["items"]} == {"confirmed"}


def test_the_customer_list_is_paged_and_deterministic(api, user):
    _orders(user, 25)
    api.force_login(user)

    first = api.get(f"{CUSTOMER_PATH}?page=1&page_size=10").json()
    again = api.get(f"{CUSTOMER_PATH}?page=1&page_size=10").json()

    assert first["total"] == 25
    assert len(first["items"]) == 10
    assert [o["id"] for o in first["items"]] == [o["id"] for o in again["items"]]
    assert [o["id"] for o in first["items"]] == sorted(
        (o["id"] for o in first["items"]), reverse=True
    )


def test_the_customer_never_sees_another_customer_through_paging(api, user):
    other = get_user_model().objects.create_user(username="+79990000099", password="Pass!2345")
    _orders(other, 5)
    mine = _orders(user, 2)
    api.force_login(user)

    body = api.get(f"{CUSTOMER_PATH}?page=1&page_size=100").json()

    assert body["total"] == 2
    assert {o["id"] for o in body["items"]} == {o.id for o in mine}


def test_orders_sharing_a_timestamp_still_page_in_a_stable_order(api, employee_user):
    from django.utils import timezone

    made = _orders(employee_user, 6)
    Order.objects.filter(pk__in=[o.pk for o in made]).update(created_at=timezone.now())
    api.force_login(employee_user)

    seen = []
    for page in (1, 2, 3):
        seen.extend(o["id"] for o in api.get(f"{EMPLOYEE_PATH}?page={page}&page_size=2").json()["items"])

    assert seen == sorted(seen, reverse=True), "tied created_at leaves the page boundary undefined"
    assert len(set(seen)) == 6


FAR = 100_000_000_000_000_000_000


@pytest.mark.parametrize(
    "path", [CUSTOMER_PATH, EMPLOYEE_PATH, "/api/employee/users", "/api/employee/users/{id}/orders"]
)
def test_a_page_past_the_offset_ceiling_is_refused_before_the_database(api, employee_user, path):
    api.force_login(employee_user)

    response = api.get(f"{path.format(id=employee_user.id)}?page={FAR}")

    assert response.status_code == 422


def test_the_deepest_page_under_the_ceiling_still_answers(api, employee_user):
    from config.pagination import MAX_OFFSET

    api.force_login(employee_user)
    deepest = MAX_OFFSET // 20 + 1

    assert api.get(f"{EMPLOYEE_PATH}?page={deepest}&page_size=20").status_code == 200
    assert api.get(f"{EMPLOYEE_PATH}?page={deepest + 1}&page_size=20").status_code == 422

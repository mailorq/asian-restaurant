"""
paging of the operations order listing

the endpoint returned every projected order, so its cost grew with the whole history
"""

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from operations import auth
from operations.models import EmployeeAuthorization, OperationOrder

pytestmark = pytest.mark.django_db

SUBJECT = 7
AUTHZ_VERSION = 3
PATH = "/ops-api/orders"


@pytest.fixture
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _known_jwks(keypair, monkeypatch):
    monkeypatch.setattr(auth, "_signing_key", lambda token: keypair.public_key())


@pytest.fixture(autouse=True)
def authorized():
    EmployeeAuthorization.objects.create(
        subject_id=SUBJECT, authz_version=AUTHZ_VERSION, role_active=True, user_active=True
    )


@pytest.fixture
def headers(keypair):
    now = int(time.time())
    token = pyjwt.encode(
        {"iss": "identity", "aud": "operations", "sub": str(SUBJECT),
         "roles": ["restaurant_employee"], "authz_version": AUTHZ_VERSION,
         "jti": "x", "iat": now, "exp": now + 600},
        keypair, algorithm="RS256",
    )
    return {"authorization": f"Bearer {token}"}


def _orders(count, status="created"):
    return [
        OperationOrder.objects.create(source_order_id=i + 1, customer_id=1, status=status)
        for i in range(count)
    ]


def test_the_listing_is_capped_even_when_the_client_asks_for_everything(headers):
    _orders(30)

    body = Client().get(f"{PATH}?page_size=100000", headers=headers).json()

    assert len(body["items"]) <= 100
    assert body["total"] == 30
    assert body["page_size"] == 100


def test_the_listing_pages_without_gaps_or_repeats(headers):
    _orders(25)
    client = Client()

    seen = []
    for page in (1, 2, 3):
        body = client.get(f"{PATH}?page={page}&page_size=10", headers=headers).json()
        seen.extend(o["source_order_id"] for o in body["items"])

    assert len(seen) == 25 and len(set(seen)) == 25


def test_orders_sharing_a_timestamp_still_page_in_a_stable_order(headers):
    from django.utils import timezone

    made = _orders(6)
    OperationOrder.objects.filter(pk__in=[o.pk for o in made]).update(created_at=timezone.now())
    client = Client()

    seen = []
    for page in (1, 2, 3):
        body = client.get(f"{PATH}?page={page}&page_size=2", headers=headers).json()
        seen.extend(o["source_order_id"] for o in body["items"])

    assert seen == sorted(seen, reverse=True), "tied created_at leaves the page boundary undefined"
    assert len(set(seen)) == 6


def test_the_status_filter_still_narrows_the_listing(headers):
    _orders(4, status="created")
    OperationOrder.objects.create(source_order_id=99, customer_id=1, status="confirmed")

    body = Client().get(f"{PATH}?status=confirmed", headers=headers).json()

    assert body["total"] == 1
    assert body["items"][0]["status"] == "confirmed"


def test_a_nonsense_page_request_is_clamped_not_rejected(headers):
    _orders(3)

    body = Client().get(f"{PATH}?page=-5&page_size=0", headers=headers).json()

    assert body["page"] == 1 and body["page_size"] >= 1 and body["items"]


def test_the_listing_still_needs_a_token():
    _orders(2)

    assert Client().get(PATH).status_code == 401


def test_a_page_past_the_offset_ceiling_is_refused_before_the_database(headers):
    from operations.pagination import MAX_OFFSET

    deepest = MAX_OFFSET // 20 + 1

    assert Client().get(f"{PATH}?page=100000000000000000000", headers=headers).status_code == 422
    assert Client().get(f"{PATH}?page={deepest}&page_size=20", headers=headers).status_code == 200
    assert Client().get(f"{PATH}?page={deepest + 1}&page_size=20", headers=headers).status_code == 422

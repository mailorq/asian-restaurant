import json
import time
import uuid

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from operations import auth
from operations.models import EmployeeAuthorization, OperationCommand

pytestmark = pytest.mark.django_db

SUBJECT = 7
AUTHZ_VERSION = 3
PATH = "/ops-api/orders/1/transition-commands"


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


def _token(keypair, sub=str(SUBJECT), authz_version=AUTHZ_VERSION):
    now = int(time.time())
    claims = {"iss": "identity", "aud": "operations", "sub": sub,
              "roles": ["restaurant_employee"], "authz_version": authz_version,
              "jti": "x", "iat": now, "exp": now + 600}
    return pyjwt.encode(claims, keypair, algorithm="RS256")


@pytest.fixture
def client():
    return Client()


def _post(client, keypair, *, key="k1", body=None, path=PATH):
    headers = {"authorization": f"Bearer {_token(keypair)}"}
    if key is not None:
        headers["idempotency-key"] = key
    payload = {"expected_status": "created", "target_status": "confirmed", "reason": "kitchen"}
    return client.post(
        path, data=json.dumps(body if body is not None else payload),
        content_type="application/json", headers=headers,
    )


def test_create_returns_202_with_a_location_that_resolves(client, keypair):
    response = _post(client, keypair)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and body["deadline_at"]

    followed = client.get(response["Location"],
                          headers={"authorization": f"Bearer {_token(keypair)}"})
    assert followed.status_code == 200
    assert followed.json()["command_id"] == body["command_id"]


def test_actor_comes_from_the_token_not_the_body(client, keypair):
    _post(client, keypair, body={"expected_status": "created", "target_status": "confirmed",
                                 "actor_id": 999, "actor_authz_version": 999})

    command = OperationCommand.objects.get()
    assert command.actor_id == SUBJECT
    assert command.payload["expected_status"] == "created"
    assert OperationCommand.objects.filter(actor_id=999).count() == 0


def test_identical_retry_returns_200_and_the_same_command(client, keypair):
    first = _post(client, keypair)
    second = _post(client, keypair)

    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["command_id"] == second.json()["command_id"]
    assert OperationCommand.objects.count() == 1


def test_same_key_with_a_different_request_is_a_conflict(client, keypair):
    _post(client, keypair)

    conflict = _post(client, keypair, body={"expected_status": "created",
                                            "target_status": "cancelled"})

    assert conflict.status_code == 409
    assert OperationCommand.objects.count() == 1


def test_missing_idempotency_key_is_rejected(client, keypair):
    response = _post(client, keypair, key=None)

    assert response.status_code == 422
    assert OperationCommand.objects.count() == 0


def test_equal_expected_and_target_is_rejected(client, keypair):
    response = _post(client, keypair, body={"expected_status": "created",
                                            "target_status": "created"})

    assert response.status_code == 422
    assert OperationCommand.objects.count() == 0


def test_unknown_status_is_rejected(client, keypair):
    response = _post(client, keypair, body={"expected_status": "created",
                                            "target_status": "teleported"})

    assert response.status_code == 422
    assert OperationCommand.objects.count() == 0


def test_anonymous_request_is_refused(client):
    response = client.post(PATH, data="{}", content_type="application/json")

    assert response.status_code == 401
    assert OperationCommand.objects.count() == 0


def test_command_of_another_actor_is_not_readable(client, keypair):
    created = _post(client, keypair).json()["command_id"]
    EmployeeAuthorization.objects.create(
        subject_id=8, authz_version=AUTHZ_VERSION, role_active=True, user_active=True
    )
    other = _token(keypair, sub="8")

    response = client.get(f"/ops-api/commands/{created}",
                          headers={"authorization": f"Bearer {other}"})

    assert response.status_code == 404


def test_unknown_command_is_not_found(client, keypair):
    response = client.get(f"/ops-api/commands/{uuid.uuid4()}",
                          headers={"authorization": f"Bearer {_token(keypair)}"})

    assert response.status_code == 404


def test_rejection_body_never_carries_contract_internals(client, keypair):
    response = _post(client, keypair, body={"expected_status": "created",
                                            "target_status": "created"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "OrderTransitionRequestedData" not in detail
    assert "pydantic" not in detail
    assert "command_id" not in detail


def test_oversized_idempotency_key_is_rejected(client, keypair):
    response = _post(client, keypair, key="x" * 201)

    assert response.status_code == 422
    assert OperationCommand.objects.count() == 0


def test_control_character_in_the_key_is_rejected(client, keypair):
    response = _post(client, keypair, key="a\x00b")

    assert response.status_code == 422
    assert OperationCommand.objects.count() == 0


def test_non_positive_order_id_is_rejected(client, keypair):
    response = _post(client, keypair, path="/ops-api/orders/0/transition-commands")

    assert response.status_code == 422
    assert "order_id" in json.dumps(response.json())
    assert OperationCommand.objects.count() == 0


def test_oversized_reason_is_rejected(client, keypair):
    response = _post(client, keypair, body={"expected_status": "created",
                                            "target_status": "confirmed",
                                            "reason": "x" * 256})

    assert response.status_code == 422
    assert "reason" in json.dumps(response.json())
    assert OperationCommand.objects.count() == 0


def test_neither_the_schema_nor_its_page_has_a_url_in_production(settings):
    from config.api import _schema_url

    settings.PRODUCTION = True
    assert _schema_url("/docs") is None and _schema_url("/openapi.json") is None

    settings.PRODUCTION = False
    assert _schema_url("/docs") == "/docs"

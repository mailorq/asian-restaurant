import json
import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from operations import auth
from operations.models import EmployeeAuthorization

pytestmark = pytest.mark.django_db

# the discard port: nothing answers, as when identity is down
UNREACHABLE = "http://127.0.0.1:9/jwks"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key, kid="current"):
    now = int(time.time())
    claims = {"iss": "identity", "aud": "operations", "sub": "7", "roles": ["restaurant_employee"],
              "authz_version": 1, "jti": "x", "iat": now, "exp": now + 600}
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def employee():
    EmployeeAuthorization.objects.create(subject_id=7, authz_version=1, role_active=True, user_active=True)


@pytest.fixture
def identity_down(monkeypatch):
    client = pyjwt.PyJWKClient(UNREACHABLE, timeout=1)
    monkeypatch.setattr(auth, "_jwks_client", client)
    return client


def _known(client, key, kid="current"):
    # what the client keeps after a successful fetch: the jwks document as it came
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": kid, "use": "sig", "alg": "RS256"}
    client.jwk_set_cache.put({"keys": [jwk]})


def test_a_key_that_cannot_be_fetched_is_a_failure_of_the_service_not_of_the_token(identity_down, client):
    token = _token(_key())
    with pytest.raises(auth.SigningKeysUnavailable):
        auth.EmployeeJWTAuth().authenticate(SimpleNamespace(), token)

    response = client.get("/ops-api/orders", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 503
    assert response["Retry-After"] == "1"


def test_a_cached_key_still_tells_a_genuine_token_from_a_forged_one(identity_down, client):
    signing = _key()
    _known(identity_down, signing)

    genuine = client.get("/ops-api/orders", HTTP_AUTHORIZATION=f"Bearer {_token(signing)}")
    forged = client.get("/ops-api/orders", HTTP_AUTHORIZATION=f"Bearer {_token(_key())}")
    assert genuine.status_code == 200
    assert forged.status_code == 401


def test_a_kid_the_cache_does_not_hold_asks_identity_and_fails_with_it(identity_down, client):
    _known(identity_down, _key(), kid="previous")
    response = client.get("/ops-api/orders", HTTP_AUTHORIZATION=f"Bearer {_token(_key(), kid='current')}")
    assert response.status_code == 503

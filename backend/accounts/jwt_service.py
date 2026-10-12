import functools
import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from django.conf import settings
from jwt.algorithms import RSAAlgorithm

from accounts.models import has_operations_role
from accounts.roles import roles_of

ISSUER = "identity"
AUDIENCE = "operations"
ALGORITHM = "RS256"


class NotAuthorized(Exception):
    pass


def _pem(inline: str, path: str) -> str | None:
    if inline:
        return inline.replace("\\n", "\n")
    if path:
        with open(path) as fh:
            return fh.read()
    return None


# keys are read and checked once per process for each configured source: they are mounted secrets that change
# only together with a restart, and a full rsa key check on every token or every public jwks request is work an
# anonymous caller could make the server repeat. a rotated key file is picked up by restarting the process
@functools.lru_cache(maxsize=4)
def _load_private_key(inline: str, path: str):
    return load_pem_private_key(_pem(inline, path).encode(), password=None)


def _private_key():
    return _load_private_key(settings.IDENTITY_JWT_PRIVATE_KEY, settings.IDENTITY_JWT_PRIVATE_KEY_FILE)


def is_employee(user) -> bool:
    return has_operations_role(user)


def issue_employee_token(user) -> tuple[str, int]:
    # fail closed at issuance: an inactive or non-staff user never receives a token
    if not user.is_active or not has_operations_role(user):
        raise NotAuthorized("user may not receive an operations token")
    ttl = settings.IDENTITY_JWT_TTL
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user.id),
        "roles": roles_of(user),
        "authz_version": user.authz_version,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl,
    }
    token = jwt.encode(claims, _private_key(), algorithm=ALGORITHM, headers={"kid": settings.IDENTITY_JWT_KID})
    return token, ttl


def _jwk(public_key, kid: str) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": kid, "use": "sig", "alg": ALGORITHM})
    return jwk


@functools.lru_cache(maxsize=4)
def _jwks(inline: str, path: str, kid: str, previous_inline: str, previous_path: str, previous_kid: str) -> str:
    # publish the previous key alongside the current one so tokens signed before a
    # rotation still verify until they expire
    keys = [_jwk(_load_private_key(inline, path).public_key(), kid)]
    previous = _pem(previous_inline, previous_path)
    if previous and previous_kid:
        keys.append(_jwk(load_pem_public_key(previous.encode()), previous_kid))
    return json.dumps({"keys": keys})


def public_jwks() -> dict:
    return json.loads(
        _jwks(
            settings.IDENTITY_JWT_PRIVATE_KEY,
            settings.IDENTITY_JWT_PRIVATE_KEY_FILE,
            settings.IDENTITY_JWT_KID,
            settings.IDENTITY_JWT_PREVIOUS_PUBLIC_KEY,
            settings.IDENTITY_JWT_PREVIOUS_PUBLIC_KEY_FILE,
            settings.IDENTITY_JWT_PREVIOUS_KID,
        )
    )

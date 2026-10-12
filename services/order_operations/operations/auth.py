import jwt
from django.conf import settings
from ninja.security import HttpBearer

ISSUER = "identity"
AUDIENCE = "operations"
EMPLOYEE_ROLE = "restaurant_employee"
STAFF_ROLES = frozenset({"restaurant_operator", "restaurant_manager", EMPLOYEE_ROLE})

_jwks_client = None


class SigningKeysUnavailable(Exception):
    """identity's keys could not be fetched, so whether a token is genuine cannot be told"""


def _client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(settings.IDENTITY_JWKS_URL)
    return _jwks_client


def _signing_key(token: str):
    return _client().get_signing_key_from_jwt(token).key


def _authorized(claims) -> bool:
    # fail closed: the local authorization projection must know this subject at the token's authz_version with an active role and user, so an unknown or stale subject is rejected and a valid old token never authorizes
    # a projection that cannot be read is a server failure, not a verdict on the token: it propagates, nothing is granted, and an exhausted pool answers 503
    from operations.models import EmployeeAuthorization

    try:
        subject = int(claims.get("sub"))
    except (TypeError, ValueError):
        return False
    if not 0 <= subject < 2**31:
        return False
    authz = EmployeeAuthorization.objects.filter(subject_id=subject).first()
    # role_active and roles come from one emitter, so disagreement is corruption rather than a grant. it is checked here as well as in the contract
    if not (authz and authz.user_active and authz.role_active and authz.authz_version == claims.get("authz_version")):
        return False
    if not authz.roles_known:
        # identity has never sent roles for this subject; the boolean is all this projection has
        return bool(authz.role_active)
    projected = set(authz.roles or [])
    # the capability must hold on both sides: a token cannot grant what identity did not
    return bool(projected & set(claims.get("roles") or []))


class EmployeeJWTAuth(HttpBearer):
    def authenticate(self, request, token: str):
        # verify signature against Identity's rotating public keys, then the standard
        # claims; the actor is taken from the token, never from client-supplied data
        try:
            key = _signing_key(token)
        except jwt.PyJWKClientConnectionError as exc:
            # a key this service has not cached and cannot fetch: a failure here, not a verdict on the token
            raise SigningKeysUnavailable from exc
        except Exception:
            return None
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
        except Exception:
            return None
        if not (set(claims.get("roles") or []) & STAFF_ROLES):
            return None
        if not _authorized(claims):
            return None
        # typed here so no handler re-parses the token; _authorized already proved sub is an int
        request.actor_id = int(claims["sub"])
        request.actor_authz_version = claims["authz_version"]
        return claims

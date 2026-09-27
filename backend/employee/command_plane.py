"""
the employee panel's way to the operations command api (adr 0001, 0002): the token is minted here per call and never
leaves the server, the key is forwarded as operations normalizes it, and nothing about commands is stored here
"""

from functools import cache

import httpx
from django.conf import settings

from accounts import jwt_service

KEY_HEADER = "Idempotency-Key"
KEY_MAX = 200


class InvalidKey(ValueError):
    pass


class NotSent(Exception):
    """no connection to operations was made, so the request certainly did nothing there"""


class OutcomeUnknown(Exception):
    """the request may have reached operations; only a repeat with the same key finds out what it did"""


@cache
def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.OPERATIONS_API_URL,
        timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        # the api is inside the stack: a proxy taken from the environment would carry the token out of it
        trust_env=False,
    )


def normalized_key(raw: str | None) -> str:
    """the rules operations applies, so a key refused there is refused here before a token is minted"""
    key = (raw or "").strip()
    if not key:
        raise InvalidKey(f"Нужен заголовок {KEY_HEADER}")
    if len(key) > KEY_MAX:
        raise InvalidKey(f"{KEY_HEADER} длиннее {KEY_MAX} символов")
    if any(ch < " " for ch in key):
        raise InvalidKey(f"{KEY_HEADER} содержит управляющие символы")
    return key


def _call(user, method: str, path: str, **kwargs) -> httpx.Response:
    token, _ = jwt_service.issue_employee_token(user)
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    try:
        return _client().request(method, path, headers=headers, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise NotSent(type(exc).__name__) from exc
    except httpx.TransportError as exc:
        raise OutcomeUnknown(type(exc).__name__) from exc


def request_transition(user, order_id: int, key: str, intent: dict) -> httpx.Response:
    return _call(
        user,
        "POST",
        f"/ops-api/orders/{order_id}/transition-commands",
        json=intent,
        headers={KEY_HEADER: key},
    )


def command(user, command_id) -> httpx.Response:
    return _call(user, "GET", f"/ops-api/commands/{command_id}")

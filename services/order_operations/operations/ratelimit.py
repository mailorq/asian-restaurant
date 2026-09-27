"""
fixed window limits on new commands per employee and per employee and order; a repeat of an existing command is answered
before the limiter. charge and create are not one transaction: a concurrent first request or a create failing after its
charge spends a unit that is never refunded, so commands created never outnumber units charged
"""

import math
from functools import cache

import redis
from django.conf import settings
from redis.backoff import NoBackoff
from redis.retry import Retry

# every counter is checked before any is spent, so a refusal costs nothing; it returns the longest wait among the exhausted ones
_CHARGE = """
local wait = 0
for i, key in ipairs(KEYS) do
  if tonumber(redis.call('GET', key) or '0') >= tonumber(ARGV[i]) then
    local ttl = redis.call('PTTL', key)
    if ttl < 0 then ttl = tonumber(ARGV[#KEYS + 1]) end
    if ttl > wait then wait = ttl end
  end
end
if wait > 0 then return wait end
for _, key in ipairs(KEYS) do
  if redis.call('INCR', key) == 1 then
    redis.call('PEXPIRE', key, ARGV[#KEYS + 1])
  end
end
return 0
"""


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class LimiterUnavailable(Exception):
    """the counters could not be read or written; the request is refused rather than let through uncounted"""


@cache
def _script():
    # one attempt with a short timeout, so a stalled store answers 503 at once
    client = redis.Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
        retry=Retry(NoBackoff(), 0),
    )
    return client.register_script(_CHARGE)


def keys(actor_id: int, order_id: int) -> list[str]:
    return [f"ops:limit:actor:{actor_id}", f"ops:limit:order:{actor_id}:{order_id}"]


def charge(actor_id: int, order_id: int) -> None:
    if not settings.REDIS_URL:
        raise LimiterUnavailable("OPERATIONS_REDIS_URL is not set")
    window_ms = settings.COMMAND_LIMIT_WINDOW_SECONDS * 1000
    limits = [settings.COMMAND_LIMIT_PER_ACTOR, settings.COMMAND_LIMIT_PER_ORDER]
    try:
        wait_ms = _script()(keys=keys(actor_id, order_id), args=[*limits, window_ms])
    except redis.RedisError as exc:
        raise LimiterUnavailable(type(exc).__name__) from exc
    if wait_ms:
        raise RateLimited(math.ceil(wait_ms / 1000))

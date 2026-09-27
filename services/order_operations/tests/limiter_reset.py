"""the limiter reset for the modules that create commands; the broker permission test must run without django or redis"""

import pytest
import redis
from django.conf import settings

from operations import ratelimit


@pytest.fixture(autouse=True)
def command_limiter():
    """every test starts with unspent limits, and only the limiter's keys are removed from the store"""
    ratelimit._script.cache_clear()
    if settings.REDIS_URL:
        store = redis.Redis.from_url(settings.REDIS_URL)
        for key in store.scan_iter("ops:limit:*"):
            store.delete(key)
    yield
    ratelimit._script.cache_clear()

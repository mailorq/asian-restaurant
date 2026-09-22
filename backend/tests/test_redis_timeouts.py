import socket
import threading
from urllib.parse import urlparse

import pytest
from asgiref.sync import async_to_sync
from ninja.errors import HttpError

from cart import service
from common import ratelimit

# a bounded client fails within its one second; redis-py's own default would wait five
DEADLINE_SECONDS = 3


class _StallingProxy:
    """forwards to the real redis until frozen, then swallows every request: a redis that stops answering mid connection"""

    def __init__(self, target_url: str) -> None:
        target = urlparse(target_url)
        self._target = (target.hostname, target.port or 6379)
        self._db = target.path
        self.frozen = threading.Event()
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self._server.getsockname()[1]}{self._db}"

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            upstream = socket.create_connection(self._target)
            threading.Thread(target=self._pump, args=(client, upstream, True), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, False), daemon=True).start()

    def _pump(self, source, sink, requests: bool) -> None:
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            if requests and self.frozen.is_set():
                continue
            try:
                sink.sendall(data)
            except OSError:
                return

    def close(self) -> None:
        self._server.close()


@pytest.fixture
def stalling_redis(settings):
    proxy = _StallingProxy(settings.CART_REDIS_URL)
    yield proxy
    proxy.close()


def _within_deadline(work):
    outcome = {}

    def run():
        try:
            work()
            outcome["result"] = "answered"
        except HttpError as exc:
            outcome["result"] = exc.status_code
        except Exception as exc:
            outcome["result"] = type(exc).__name__

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=DEADLINE_SECONDS)
    assert not thread.is_alive(), "the request waited on a stalled redis instead of failing"
    return outcome["result"]


def test_the_checkout_cart_read_fails_fast_when_redis_stalls(settings, stalling_redis):
    settings.CART_REDIS_URL = stalling_redis.url
    service._sync_client = None

    def warm_then_stall():
        service.read_sync("cart:u:1")
        stalling_redis.frozen.set()
        service.read_sync("cart:u:1")

    assert _within_deadline(warm_then_stall) == 503
    service._sync_client = None


def test_the_cart_api_read_fails_fast_when_redis_stalls(settings, stalling_redis):
    settings.CART_REDIS_URL = stalling_redis.url
    service._clients.clear()

    async def warm_then_stall():
        await service.read("cart:u:1")
        stalling_redis.frozen.set()
        await service.read("cart:u:1")

    assert _within_deadline(lambda: async_to_sync(warm_then_stall)()) == 503
    service._clients.clear()


def test_the_production_cache_uses_the_bounded_client():
    from config import settings as production

    assert production.CACHES["default"]["OPTIONS"] is production.REDIS_CLIENT_OPTIONS


def test_the_rate_limit_fails_fast_when_redis_stalls(settings, stalling_redis):
    # the suite runs on a local memory cache; this builds the redis cache production configures
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": stalling_redis.url,
            "OPTIONS": settings.REDIS_CLIENT_OPTIONS,
        }
    }

    def warm_then_stall():
        ratelimit._hit("rl:test:stall", 60)
        stalling_redis.frozen.set()
        ratelimit._hit("rl:test:stall", 60)

    assert _within_deadline(warm_then_stall) == "TimeoutError"

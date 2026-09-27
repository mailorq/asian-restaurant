import functools
import inspect

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from ninja.errors import HttpError


def _client_ip(request) -> str:
    # X-Forwarded-For is client-controlled and spoofable unless a trusted reverse
    # proxy sanitizes it, so only honour it when the operator opts in.
    if getattr(settings, "RATELIMIT_TRUST_XFF", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _hit(bucket: str, window: int) -> int:
    if cache.add(bucket, 1, window):
        return 1
    try:
        return cache.incr(bucket)
    except ValueError:
        cache.set(bucket, 1, window)
        return 1


class Throttled(HttpError):
    """answered with retry after set to the window, the longest a caller can have to wait"""

    def __init__(self, window: int) -> None:
        super().__init__(429, "Слишком много запросов. Попробуйте позже.")
        self.retry_after = window


def _guard(hits: int, limit: int, window: int) -> None:
    if hits > limit:
        raise Throttled(window)


def rate_limit(scope: str, limit: int, window: int, per_user: bool = False):
    def bucket_of(request) -> str:
        return (
            f"rl:{scope}:user:{request.auth.pk}"
            if per_user
            else f"rl:{scope}:{_client_ip(request)}"
        )

    def decorator(view):
        if inspect.iscoroutinefunction(view):

            @functools.wraps(view)
            async def awrapper(request, *args, **kwargs):
                bucket = bucket_of(request)
                try:
                    hits = await sync_to_async(_hit)(bucket, window)
                except Exception as exc:
                    raise HttpError(503, "Сервис временно недоступен. Повторите позже.") from exc
                _guard(hits, limit, window)
                return await view(request, *args, **kwargs)

            return awrapper

        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            bucket = bucket_of(request)
            try:
                hits = _hit(bucket, window)
            except Exception as exc:
                raise HttpError(503, "Сервис временно недоступен. Повторите позже.") from exc
            _guard(hits, limit, window)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator

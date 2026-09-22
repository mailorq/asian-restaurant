from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import JsonResponse
from ninja.utils import check_csrf

API_PREFIX = "/api/"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _refusal(request) -> JsonResponse | None:
    if request.method in SAFE_METHODS or not request.path_info.startswith(API_PREFIX):
        return None
    # read first: the csrf check below parses a form token out of the stream, after which the body is gone
    body = request.body
    # ninja exempts every view from django's csrf check and repeats it only for cookie auth, so a write open to guests (login, registration, the guest cart) is checked here or nowhere
    if check_csrf(request) is not None:
        return JsonResponse({"detail": "CSRF-проверка не пройдена. Обновите страницу"}, status=403)
    # the parser takes any body for json, and a plain form is the one body another site can post without asking
    if body and request.content_type != "application/json":
        return JsonResponse({"detail": "Ожидается тело application/json"}, status=415)
    return None


class ApiWriteGuard:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response) -> None:
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.is_async:
            return self._acall(request)
        return _refusal(request) or self.get_response(request)

    async def _acall(self, request):
        return _refusal(request) or await self.get_response(request)

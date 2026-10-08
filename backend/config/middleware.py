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


# the endpoints that find out or deliberately change who is signed in; logout and the employee token act
# for the signed-in account and are checked like everything else
IDENTITY_PATHS = frozenset(
    {
        "/api/auth/me",
        "/api/auth/csrf",
        "/api/auth/jwks",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/register/code",
        "/api/auth/password/code",
        "/api/auth/password/reset",
    }
)


def _claimed(request) -> str | None:
    path = request.path_info.rstrip("/")
    if not request.path_info.startswith(API_PREFIX) or path in IDENTITY_PATHS:
        return None
    return request.headers.get("X-Account")


def _account_changed(claimed: str, user) -> JsonResponse | None:
    signed_in = str(user.pk) if user.is_authenticated else "guest"
    if claimed == signed_in:
        return None
    return JsonResponse(
        {
            "code": "account_changed",
            "detail": "Вход выполнен под другим аккаунтом, действие не выполнено",
        },
        status=409,
    )


class AccountGuard:
    """refuses a request made for an account that is not the one signed in

    tabs share cookies, so a request leaves with whoever signed in last, in any tab. the spa names the
    account it shows, and a request for another one is refused before its view runs: nothing is read or
    written for an account the page does not show. a client that names none is served as before
    """

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
        claimed = _claimed(request)
        refusal = _account_changed(claimed, request.user) if claimed is not None else None
        return refusal or self.get_response(request)

    async def _acall(self, request):
        claimed = _claimed(request)
        refusal = _account_changed(claimed, await request.auser()) if claimed is not None else None
        return refusal or await self.get_response(request)

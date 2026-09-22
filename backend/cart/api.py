import uuid

from django.conf import settings
from django.http import HttpResponse
from ninja import Router, Status
from ninja.errors import HttpError

from cart import service
from cart.schemas import AddItemIn, CartConflictOut, CartOut, SetQtyIn
from common.ratelimit import rate_limit
from menu.models import Product

router = Router(tags=["cart"])

WRITE_LIMIT = dict(scope="cart_write", limit=60, window=60)

COOKIE_NAME = "cartid"
COOKIE_SALT = "cart"
COOKIE_PATH = "/api/cart"

def _read_cookie(request) -> str | None:
    try:
        return request.get_signed_cookie(
            COOKIE_NAME, default=None, salt=COOKIE_SALT, max_age=service.CART_TTL
        )
    except Exception:
        return None


def _set_cookie(response: HttpResponse, cart_id: str) -> None:
    response.set_signed_cookie(
        COOKIE_NAME,
        cart_id,
        salt=COOKIE_SALT,
        max_age=service.CART_TTL,
        httponly=True,
        samesite="Lax",
        secure=not settings.DEBUG,
        path=COOKIE_PATH,
    )


async def _resolve(request, response: HttpResponse) -> str:
    """Return the Redis cart key for this request, managing the guest cookie.

    Authenticated requests use a per-user key and absorb any lingering guest
    cart (merge, then drop the cookie). Guests get a signed, opaque cookie id.
    """
    user = await request.auser()
    if user.is_authenticated:
        key = service.user_key(user.id)
        guest_id = _read_cookie(request)
        if guest_id:
            await service.merge_guest_into_user(service.guest_key(guest_id), key)
            response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
        return key

    guest_id = _read_cookie(request) or uuid.uuid4().hex
    _set_cookie(response, guest_id)
    return service.guest_key(guest_id)


async def _current(key: str) -> dict:
    items, version = await service.read(key)
    return await service.render(key, items, version)


async def _conflict(key: str) -> dict:
    return {"code": "cart_version_conflict", "cart": await _current(key)}


@router.get("", response=CartOut)
async def get_cart(request, response: HttpResponse):
    key = await _resolve(request, response)
    return await _current(key)


@router.post("/items", response={200: CartOut, 409: CartConflictOut})
@rate_limit(**WRITE_LIMIT)
async def add_item(request, data: AddItemIn, response: HttpResponse):
    if not await Product.objects.filter(id=data.product_id, is_active=True).aexists():
        raise HttpError(404, "Товар недоступен")
    key = await _resolve(request, response)
    try:
        await service.add(key, data.product_id, data.quantity, data.expected_version)
    except service.CartConflict:
        return Status(409, await _conflict(key))
    return Status(200, await _current(key))


@router.put("/items/{product_id}", response={200: CartOut, 409: CartConflictOut})
@rate_limit(**WRITE_LIMIT)
async def set_item(request, product_id: int, data: SetQtyIn, response: HttpResponse):
    if data.quantity > 0 and not await Product.objects.filter(id=product_id, is_active=True).aexists():
        raise HttpError(404, "Товар недоступен")
    key = await _resolve(request, response)
    try:
        await service.set_qty(key, product_id, data.quantity, data.expected_version)
    except service.CartConflict:
        return Status(409, await _conflict(key))
    return Status(200, await _current(key))


@router.delete("/items/{product_id}", response={200: CartOut, 409: CartConflictOut})
@rate_limit(**WRITE_LIMIT)
async def remove_item(request, product_id: int, response: HttpResponse, expected_version: int | None = None):
    key = await _resolve(request, response)
    try:
        await service.remove(key, product_id, expected_version)
    except service.CartConflict:
        return Status(409, await _conflict(key))
    return Status(200, await _current(key))


@router.delete("", response={200: CartOut, 409: CartConflictOut})
@rate_limit(**WRITE_LIMIT)
async def clear_cart(request, response: HttpResponse, expected_version: int | None = None):
    key = await _resolve(request, response)
    try:
        await service.clear(key, expected_version)
    except service.CartConflict:
        return Status(409, await _conflict(key))
    return Status(200, await _current(key))

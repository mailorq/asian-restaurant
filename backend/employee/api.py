import logging
import uuid

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.http import HttpResponse
from ninja import Router, Status
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts import jwt_service
from accounts.roles import InvalidRoleTarget, NotAuthorized, set_staff_role
from common.ratelimit import rate_limit
from config.pagination import DEFAULT_PAGE_SIZE, paginate
from employee import command_plane
from employee.permissions import (
    customers_required,
    employee_required,
    inventory_required,
    superuser_required,
)
from employee.schemas import (
    ORDERS_PREVIEW,
    AdjustIn,
    CommandOut,
    EmployeeUserOut,
    InventoryConflictOut,
    InventoryItemOut,
    OrderScope,
    OrderSort,
    PagedCustomerOrders,
    PagedUsers,
    RefusalOut,
    RoleIn,
    StockAdjustmentOut,
    TransitionCommandIn,
    TransitionIn,
    UserDetailOut,
)
from menu import inventory as inventory_service
from menu.models import Product, StockAdjustment
from orders import service as order_service
from orders.models import ACTIVE_ORDER_STATUSES, Order
from orders.schemas import OrderOut, PagedOrders

router = Router(tags=["employee"], auth=django_auth)
logger = logging.getLogger(__name__)

HISTORY_MAX_PAGE_SIZE = 50


def _orders_qs():
    return Order.objects.select_related("delivery_address").prefetch_related("items")


# orders
@router.get("/orders", response=PagedOrders)
@employee_required
def list_orders(request, status: str | None = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    # id breaks ties on equal timestamps; offset paging still shifts when the set changes
    qs = _orders_qs().order_by("-created_at", "-id")
    if status:
        qs = qs.filter(status=status)
    return paginate(qs, page, page_size)


@router.get("/orders/{order_id}", response=OrderOut)
@employee_required
def order_detail(request, order_id: int):
    order = _orders_qs().filter(id=order_id).first()
    if order is None:
        raise HttpError(404, "Заказ не найден")
    return order


# a tab opened before an employee was switched either way learns it from this code and reloads the way it changes status
MODE_CHANGED = "transition_mode_changed"

@router.post("/orders/{order_id}/transition", response={200: OrderOut, 403: RefusalOut})
@employee_required
def transition_order(request, order_id: int, data: TransitionIn):
    if request.auth.transitions_via_commands:
        detail = "Статус заказа меняется командой, прямой переход для вас закрыт"
        return Status(403, {"detail": detail, "code": MODE_CHANGED})
    order = Order.objects.filter(id=order_id).first()
    if order is None:
        raise HttpError(404, "Заказ не найден")
    try:
        order_service.transition(
            order, data.to_status, changed_by=request.auth, note=data.note, expected_status=data.expected_status
        )
    except order_service.CheckoutError as exc:
        raise HttpError(409 if exc.code == "stale_order" else 400, exc.message) from exc
    return _orders_qs().get(pk=order.pk)


# the answers of operations are relayed as they are and never retried here
COMMAND_ANSWERS = {
    200: CommandOut,
    202: CommandOut,
    403: RefusalOut,
    429: RefusalOut,
    503: RefusalOut,
    504: RefusalOut,
}


def _relayed(reply, response: HttpResponse):
    if reply.status_code in (200, 202):
        body = reply.json()
        response["Location"] = f"/api/employee/commands/{body['command_id']}"
        return Status(reply.status_code, body)
    if reply.status_code in (429, 503):
        response["Retry-After"] = reply.headers.get("Retry-After", "1")
        detail = (
            "Слишком много новых действий, повторите позже"
            if reply.status_code == 429
            else "Сервис операций временно недоступен"
        )
        return Status(reply.status_code, {"detail": detail})
    if reply.status_code == 404:
        raise HttpError(404, "Команда не найдена")
    if reply.status_code == 409:
        raise HttpError(409, "Этот ключ уже использован для другого действия")
    if reply.status_code == 422:
        raise HttpError(422, "Сервис операций не принял запрос")
    if reply.status_code == 401:
        raise HttpError(
            403, "Права изменились или еще не дошли до сервиса операций, действие не выполнено"
        )
    logger.error("operations answered %s to %s", reply.status_code, reply.request.url.path)
    raise HttpError(502, "Сервис операций ответил ошибкой")


def _forwarded(call, response: HttpResponse):
    try:
        return _relayed(call(), response)
    except jwt_service.NotAuthorized as exc:
        raise HttpError(403, "Недостаточно прав") from exc
    except command_plane.NotSent:
        response["Retry-After"] = "1"
        return Status(
            503,
            {"detail": "Сервис операций недоступен, действие не отправлено", "code": "not_sent"},
        )
    except command_plane.OutcomeUnknown:
        detail = "Исход неизвестен. Повторите то же действие: второй раз оно не выполнится"
        return Status(504, {"detail": detail, "code": "outcome_unknown"})


@router.post("/orders/{order_id}/transition-commands", response=COMMAND_ANSWERS)
@employee_required
@rate_limit("employee-commands", limit=60, window=60, per_user=True)
def request_transition_command(
    request, response: HttpResponse, order_id: int, data: TransitionCommandIn
):
    if not request.auth.transitions_via_commands:
        return Status(403, {"detail": "Для вас статус заказа меняется напрямую", "code": MODE_CHANGED})
    try:
        key = command_plane.normalized_key(request.headers.get(command_plane.KEY_HEADER))
    except command_plane.InvalidKey as exc:
        raise HttpError(422, str(exc)) from exc
    intent = {
        "expected_status": data.expected_status,
        "target_status": data.target_status,
        "reason": data.reason,
    }
    return _forwarded(
        lambda: command_plane.request_transition(request.auth, order_id, key, intent), response
    )


@router.get("/commands/{command_id}", response=COMMAND_ANSWERS)
@employee_required
@rate_limit("employee-command-status", limit=240, window=60, per_user=True)
def command_status(request, response: HttpResponse, command_id: uuid.UUID):
    # operations answers only its author, so another employee's command is a 404 there
    return _forwarded(lambda: command_plane.command(request.auth, command_id), response)


# inventory
@router.get("/inventory", response=list[InventoryItemOut])
@inventory_required
def inventory(request, search: str | None = None):
    qs = Product.objects.all().order_by("category", "name")
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
    return qs


@router.post(
    "/inventory/{product_id}/adjust", response={200: InventoryItemOut, 409: InventoryConflictOut}
)
@inventory_required
def adjust_stock(request, product_id: int, data: AdjustIn):
    try:
        product = inventory_service.set_stock(
            product_id,
            data.new_quantity,
            reason=data.reason,
            staff=request.auth,
            expected_version=data.expected_version,
        )
    except inventory_service.StaleProduct as exc:
        current = exc.product
        return Status(
            409,
            {
                "code": "stock_version_conflict",
                "detail": f"Остаток уже изменился: сейчас {current.stock_quantity} шт. Проверьте и сохраните снова",
                "product": current,
            },
        )
    if product is None:
        raise HttpError(404, "Товар не найден")
    return Status(200, product)


@router.get("/inventory/{product_id}/adjustments", response=list[StockAdjustmentOut])
@inventory_required
def stock_adjustments(request, product_id: int):
    return list(StockAdjustment.objects.filter(product_id=product_id).select_related("staff")[:50])


# users
@router.get("/users", response=PagedUsers)
@customers_required
def list_users(request, search: str | None = None, page: int = 1, page_size: int = 20):
    qs = (
        get_user_model()
        .objects.prefetch_related("groups")
        .annotate(
            active_orders_count=Count("orders", filter=Q(orders__status__in=ACTIVE_ORDER_STATUSES))
        )
        .order_by("-date_joined")
    )
    if search:
        qs = qs.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(phone__icontains=search)
        )
    return paginate(qs, page, page_size)


@router.get("/users/{user_id}", response=UserDetailOut)
@customers_required
def user_detail(request, user_id: int):
    user = (
        get_user_model().objects.annotate(orders_total=Count("orders")).filter(id=user_id).first()
    )
    if user is None:
        raise HttpError(404, "Пользователь не найден")
    user.orders_preview = _customer_orders(user_id)[:ORDERS_PREVIEW]
    return user


def _customer_orders(
    user_id: int, scope: OrderScope = OrderScope.ALL, sort: OrderSort = OrderSort.NEWEST
):
    """
    one ordering for both the preview and the paged history

    id breaks ties on equal timestamps, so a page boundary is defined for a fixed set. offset
    paging still shifts when the set changes underneath, which a cursor would solve if strict
    continuity is ever needed. nothing here selects related rows: the shape carries no items
    and no address
    """
    orders = Order.objects.filter(user_id=user_id)
    if scope is OrderScope.ACTIVE:
        orders = orders.filter(status__in=ACTIVE_ORDER_STATUSES)
    elif scope is OrderScope.HISTORY:
        orders = orders.exclude(status__in=ACTIVE_ORDER_STATUSES)
    ascending = sort is OrderSort.OLDEST
    return (
        orders.order_by("created_at", "id") if ascending else orders.order_by("-created_at", "-id")
    )


@router.get("/users/{user_id}/orders", response=PagedCustomerOrders)
@customers_required
def user_orders(
    request,
    user_id: int,
    scope: OrderScope = OrderScope.ALL,
    sort: OrderSort = OrderSort.NEWEST,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
):
    if not get_user_model().objects.filter(id=user_id).exists():
        raise HttpError(404, "Пользователь не найден")
    return paginate(
        _customer_orders(user_id, scope, sort), page, page_size, max_page_size=HISTORY_MAX_PAGE_SIZE
    )


@router.post("/users/{user_id}/role", response=EmployeeUserOut)
@superuser_required
def set_role(request, user_id: int, data: RoleIn):
    target = get_user_model().objects.filter(id=user_id).first()
    if target is None:
        raise HttpError(404, "Пользователь не найден")
    try:
        set_staff_role(actor=request.auth, target=target, role=data.role)
    except InvalidRoleTarget as exc:
        raise HttpError(409, "Роль суперпользователя меняется отдельно") from exc
    except NotAuthorized as exc:
        raise HttpError(403, str(exc)) from exc
    return (
        get_user_model()
        .objects.prefetch_related("groups")
        .annotate(
            active_orders_count=Count("orders", filter=Q(orders__status__in=ACTIVE_ORDER_STATUSES))
        )
        .get(pk=target.pk)
    )

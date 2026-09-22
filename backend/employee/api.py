from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from ninja import Router, Status
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts.roles import InvalidRoleTarget, NotAuthorized, set_staff_role
from config.pagination import DEFAULT_PAGE_SIZE, paginate
from employee.permissions import (
    customers_required,
    employee_required,
    inventory_required,
    superuser_required,
)
from employee.schemas import (
    ORDERS_PREVIEW,
    AdjustIn,
    EmployeeUserOut,
    InventoryConflictOut,
    InventoryItemOut,
    OrderScope,
    OrderSort,
    PagedCustomerOrders,
    PagedUsers,
    RoleIn,
    StockAdjustmentOut,
    TransitionIn,
    UserDetailOut,
)
from menu import inventory as inventory_service
from menu.models import Product, StockAdjustment
from orders import service as order_service
from orders.models import ACTIVE_ORDER_STATUSES, Order
from orders.schemas import OrderOut, PagedOrders

router = Router(tags=["employee"], auth=django_auth)

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


@router.post("/orders/{order_id}/transition", response=OrderOut)
@employee_required
def transition_order(request, order_id: int, data: TransitionIn):
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


# inventory
@router.get("/inventory", response=list[InventoryItemOut])
@inventory_required
def inventory(request, search: str | None = None):
    qs = Product.objects.all().order_by("category", "name")
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
    return qs


@router.post("/inventory/{product_id}/adjust", response={200: InventoryItemOut, 409: InventoryConflictOut})
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
            Q(username__icontains=search) | Q(first_name__icontains=search) | Q(phone__icontains=search)
        )
    return paginate(qs, page, page_size)


@router.get("/users/{user_id}", response=UserDetailOut)
@customers_required
def user_detail(request, user_id: int):
    user = (
        get_user_model().objects
        .annotate(orders_total=Count("orders"))
        .filter(id=user_id)
        .first()
    )
    if user is None:
        raise HttpError(404, "Пользователь не найден")
    user.orders_preview = _customer_orders(user_id)[:ORDERS_PREVIEW]
    return user


def _customer_orders(user_id: int, scope: OrderScope = OrderScope.ALL, sort: OrderSort = OrderSort.NEWEST):
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
    return orders.order_by("created_at", "id") if ascending else orders.order_by("-created_at", "-id")


@router.get("/users/{user_id}/orders", response=PagedCustomerOrders)
@customers_required
def user_orders(request, user_id: int, scope: OrderScope = OrderScope.ALL, sort: OrderSort = OrderSort.NEWEST, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    if not get_user_model().objects.filter(id=user_id).exists():
        raise HttpError(404, "Пользователь не найден")
    return paginate(_customer_orders(user_id, scope, sort), page, page_size,
                    max_page_size=HISTORY_MAX_PAGE_SIZE)


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

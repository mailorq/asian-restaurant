from datetime import datetime
from enum import StrEnum

from ninja import Field, Schema
from pydantic import ConfigDict

from accounts.roles import StaffRole, staff_role
from employee.permissions import is_employee
from orders.models import ACTIVE_ORDER_STATUSES


class TransitionIn(Schema):
    to_status: str
    note: str = Field(default="", max_length=255)
    expected_status: str | None = None  # optimistic guard against stale actions


class InventoryItemOut(Schema):
    id: int
    code: str
    name: str
    category: str
    stock_quantity: int
    is_active: bool
    version: int


class AdjustIn(Schema):
    new_quantity: int = Field(ge=0)
    reason: str = Field(min_length=2, max_length=255)
    # the version the employee saw: a sale or another adjustment since then refuses the write
    expected_version: int = Field(ge=1)


class InventoryConflictOut(Schema):
    code: str
    detail: str
    product: InventoryItemOut


class StockAdjustmentOut(Schema):
    old_quantity: int
    new_quantity: int
    reason: str
    staff: str | None
    created_at: datetime

    @staticmethod
    def resolve_staff(obj) -> str | None:
        return obj.staff.username if obj.staff_id else None


class EmployeeUserOut(Schema):
    id: int
    username: str
    name: str
    phone: str | None
    is_employee: bool
    is_superuser: bool
    staff_role: StaffRole | None
    active_orders_count: int
    date_joined: datetime

    @staticmethod
    def resolve_name(obj) -> str:
        return obj.first_name

    @staticmethod
    def resolve_is_employee(obj) -> bool:
        # prefetch friendly (avoids a query per row when groups are prefetched)
        return is_employee(obj)

    @staticmethod
    def resolve_staff_role(obj):
        return staff_role(obj)

    @staticmethod
    def resolve_active_orders_count(obj) -> int:
        value = getattr(obj, "active_orders_count", None)
        if value is not None:
            return value
        return obj.orders.filter(status__in=ACTIVE_ORDER_STATUSES).count()


class PagedUsers(Schema):
    items: list[EmployeeUserOut]
    total: int
    page: int
    page_size: int


ORDERS_PREVIEW = 5


class CustomerOrderPreviewOut(Schema):
    id: int
    status: str
    total: float
    created_at: datetime

    @staticmethod
    def resolve_total(obj) -> float:
        return float(obj.total)


class UserDetailOut(Schema):
    id: int
    username: str
    name: str
    phone: str | None
    is_employee: bool
    is_superuser: bool
    staff_role: StaffRole | None
    orders_total: int
    orders_preview: list[CustomerOrderPreviewOut]

    @staticmethod
    def resolve_name(obj) -> str:
        return obj.first_name

    @staticmethod
    def resolve_is_employee(obj) -> bool:
        return is_employee(obj)

    @staticmethod
    def resolve_staff_role(obj):
        return staff_role(obj)


class PagedCustomerOrders(Schema):
    items: list[CustomerOrderPreviewOut]
    total: int
    page: int
    page_size: int


class OrderScope(StrEnum):
    ACTIVE = "active"
    HISTORY = "history"
    ALL = "all"


class OrderSort(StrEnum):
    NEWEST = "created_at_desc"
    OLDEST = "created_at_asc"


class RoleIn(Schema):
    model_config = ConfigDict(extra="forbid")
    # required but nullable: an empty body must not quietly mean "revoke"
    role: StaffRole | None

from django.contrib import admin, messages

from orders import service as order_service
from orders.models import (
    DeliveryAddress,
    Order,
    OrderItem,
    OrderOutbox,
    OrderStatusHistory,
    ProcessedEvent,
)

_STATUS_LABELS = dict(Order.Status.choices)


class _AuditOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderOutbox)
class OrderOutboxAdmin(_AuditOnly):
    list_display = ("aggregate_id", "event_type", "status", "attempts", "created_at", "published_at")
    list_filter = ("status", "event_type")
    search_fields = ("aggregate_id",)


@admin.register(ProcessedEvent)
class ProcessedEventAdmin(_AuditOnly):
    list_display = ("event_id", "consumer", "processed_at")
    search_fields = ("event_id",)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product", "product_name", "unit_price", "quantity", "line_total")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ("from_status", "to_status", "changed_by", "note", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


def _make_transition_action(target: str):
    def action(modeladmin, request, queryset):
        if request.user.transitions_via_commands:
            messages.error(
                request,
                "Статус заказа меняется командой из панели сотрудника, переход из админки для вас закрыт",
            )
            return
        done = 0
        for order in queryset:
            try:
                order_service.transition(order, target, changed_by=request.user, expected_status=order.status)
                done += 1
            except order_service.CheckoutError as exc:
                messages.warning(request, f"#{order.id}: {exc.message}")
        if done:
            messages.success(request, f"Переведено заказов: {done}")

    action.__name__ = f"transition_to_{target}"
    action.short_description = f"Перевести в «{_STATUS_LABELS[target]}»"
    return action


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "payment_method", "total", "created_at")
    list_filter = ("status", "payment_method")
    search_fields = ("id", "phone", "user__username")
    # status changes only through order_service.transition (writes history + outbox);
    # every business field is view-only here
    readonly_fields = (
        "user", "status", "payment_method", "phone", "contact_name", "delivery_address",
        "total", "idempotency_key", "source_cart_id", "source_cart_version", "created_at", "updated_at",
    )
    inlines = (OrderItemInline, OrderStatusHistoryInline)
    actions = [_make_transition_action(s) for s in Order.Status.values]

    def has_add_permission(self, request):
        # orders exist only as a result of checkout
        return False

    def has_delete_permission(self, request, obj=None):
        # a deleted order would silently diverge from operations (no event, no history)
        return False


@admin.register(DeliveryAddress)
class DeliveryAddressAdmin(_AuditOnly):
    # delivery addresses are an order's immutable snapshot of where it ships
    list_display = ("id", "user", "address", "is_verified", "created_at")
    list_filter = ("is_verified", "provider")
    search_fields = ("address", "user__username")

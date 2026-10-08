import { useState } from "react";
import { formatUaPhone } from "../../lib/phone";
import { Icon } from "../Icon";
import { useToast } from "../../stores/toast";
import { ORDER_STATUS, formatOrderDate, type OrderStatus } from "../../api/orders";
import {
  commandVerdict,
  useDismissCommand,
  useEmployeeOrders,
  usePendingCommands,
  useResumeCommand,
  useTransitionCommand,
  useTransitionOrder,
} from "../../api/employee";
import { modeChanged, type Intent } from "../../lib/commandIntents";
import { useAuth } from "../../stores/auth";
import { formatPrice } from "../../lib/menu";

const NEXT_ACTIONS: Record<OrderStatus, OrderStatus[]> = {
  created: ["confirmed", "cancelled"],
  confirmed: ["preparing", "cancelled"],
  preparing: ["delivering", "cancelled"],
  delivering: ["delivered", "cancelled"],
  delivered: [],
  cancelled: [],
};

const FILTERS: { value: OrderStatus | ""; label: string }[] = [
  { value: "", label: "Все" },
  { value: "created", label: "Созданы" },
  { value: "confirmed", label: "Подтверждены" },
  { value: "preparing", label: "Готовятся" },
  { value: "delivering", label: "В доставке" },
  { value: "delivered", label: "Доставлены" },
  { value: "cancelled", label: "Отменены" },
];

export function EmployeeOrders() {
  const [filter, setFilter] = useState<OrderStatus | "">("");
  const [page, setPage] = useState(1);
  const { data, isLoading, isError, refetch } = useEmployeeOrders(filter, page);
  const orders = data?.items;
  const total = data?.total ?? 0;
  const pageSize = data?.page_size ?? 20;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const userId = useAuth((s) => s.user?.id ?? 0);
  const byCommands = useAuth((s) => Boolean(s.user?.transitions_via_commands));
  const direct = useTransitionOrder();
  const viaCommand = useTransitionCommand(userId);
  const resumeCommand = useResumeCommand(userId);
  const dismissCommand = useDismissCommand(userId);
  const unresolved = usePendingCommands(userId).data ?? [];
  const pending = direct.isPending || viaCommand.isPending || resumeCommand.isPending;
  const notify = useToast((s) => s.notify);

  function told(intent: Intent) {
    const verdict = commandVerdict(intent);
    notify(`Заказ №${intent.orderId}: ${verdict.text.toLowerCase()}`, verdict.ok ? undefined : "error");
  }

  // no retry here: the switch decides which way is allowed, and the employee repeats the action on the right one
  function failed(e: unknown) {
    if (modeChanged(e)) {
      useAuth.getState().refresh();
      notify("Способ смены статуса изменился, повторите действие", "error");
      return;
    }
    notify(e instanceof Error ? e.message : "Не удалось изменить статус", "error");
  }

  function act(orderId: number, from: OrderStatus, to: OrderStatus) {
    if (byCommands) {
      viaCommand.mutate({ orderId, to_status: to, expected_status: from }, { onSuccess: told, onError: failed });
      return;
    }
    direct.mutate(
      { orderId, to_status: to, expected_status: from },
      { onSuccess: () => notify(`Заказ №${orderId}: ${ORDER_STATUS[to].label.toLowerCase()}`), onError: failed },
    );
  }

  return (
    <div>
      {unresolved.length > 0 && (
        <ul className="mb-5 flex flex-col gap-2">
          {unresolved.map((intent) => (
            <li
              key={intent.key}
              className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-500/40 px-4 py-3 text-sm"
            >
              <span>
                Заказ №{intent.orderId}: «{ORDER_STATUS[intent.to_status as OrderStatus].label}» -{" "}
                {commandVerdict(intent).text.toLowerCase()}
              </span>
              <span className="flex gap-2">
                <button
                  onClick={() => resumeCommand.mutate(intent.key, { onSuccess: told, onError: failed })}
                  disabled={pending}
                  className="btn btn-primary h-9 min-h-9 px-4 text-sm"
                >
                  {intent.commandId ? "Обновить" : "Продолжить"}
                </button>
                <button
                  onClick={() => dismissCommand(intent.key)}
                  className="btn btn-secondary h-9 min-h-9 px-4 text-sm text-muted"
                >
                  Скрыть
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="mb-5 flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.value || "all"}
            onClick={() => {
              setFilter(f.value);
              setPage(1);
            }}
            className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors ${
              filter === f.value
                ? "bg-primary text-primary-contrast"
                : "border border-border text-muted hover:text-text"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {isLoading ? (
        <ul className="flex flex-col gap-3">
          {[0, 1, 2].map((i) => (
            <li key={i} className="h-28 animate-pulse rounded-2xl bg-surface-2" />
          ))}
        </ul>
      ) : isError ? (
        <div className="rounded-2xl border border-border py-12 text-center">
          <p className="text-sm text-muted">Не удалось загрузить заказы.</p>
          <button onClick={() => refetch()} className="mt-3 font-medium text-accent hover:underline">
            Повторить
          </button>
        </div>
      ) : !orders || orders.length === 0 ? (
        <p className="rounded-2xl border border-border py-12 text-center text-sm text-muted">
          Заказов нет
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {orders.map((order) => {
            const status = ORDER_STATUS[order.status];
            return (
              <li key={order.id} className="rounded-2xl border border-border p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold">Заказ №{order.id}</span>
                  <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${status.cls}`}>
                    {status.label}
                  </span>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
                  <span className="flex items-center gap-1">
                    <Icon name="clock" size={12} /> {formatOrderDate(order.created_at)}
                  </span>
                  <span>{order.contact_name}</span>
                  <span className="tnum">{formatUaPhone(order.phone)}</span>
                  <span className="flex items-center gap-1">
                    <Icon name="pin" size={12} /> {order.address}
                  </span>
                  {!order.address_verified && (
                    <span className="font-medium text-amber-600 dark:text-amber-400">
                      адрес не подтвержден, проверьте
                    </span>
                  )}
                </div>
                <p className="mt-2 text-sm text-muted">
                  {order.items.map((i) => (i.quantity > 1 ? `${i.name} ×${i.quantity}` : i.name)).join(", ")}
                </p>
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
                  <div className="flex flex-wrap gap-2">
                    {NEXT_ACTIONS[order.status].map((to) => (
                      <button
                        key={to}
                        onClick={() => act(order.id, order.status, to)}
                        disabled={pending}
                        className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 ${
                          to === "cancelled"
                            ? "border border-danger/40 text-danger hover:bg-danger/10"
                            : "bg-primary text-primary-contrast hover:bg-primary-hover"
                        }`}
                      >
                        {to === "cancelled" ? "Отменить" : ORDER_STATUS[to].label}
                      </button>
                    ))}
                  </div>
                  <span className="tnum font-semibold">{formatPrice(order.total)}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {total > pageSize && (
        <div className="mt-5 flex items-center justify-between text-sm text-muted">
          <span>Всего: {total}</span>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="grid h-9 w-9 place-items-center rounded-full border border-border hover:text-text disabled:opacity-40"
              aria-label="Назад"
            >
              <Icon name="arrowLeft" size={16} />
            </button>
            <span className="tnum">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="grid h-9 w-9 place-items-center rounded-full border border-border hover:text-text disabled:opacity-40"
              aria-label="Вперед"
            >
              <Icon name="arrowRight" size={16} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

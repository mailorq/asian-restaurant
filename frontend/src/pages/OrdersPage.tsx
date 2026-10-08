import { useState } from "react";
import { Icon } from "../components/Icon";
import { useUI } from "../stores/ui";
import { useAuth } from "../stores/auth";
import { ORDER_STATUS, formatOrderDate, useOrders } from "../api/orders";
import { formatPrice } from "../lib/menu";

export function OrdersPage() {
  const openModal = useUI((s) => s.openModal);
  const navigate = useUI((s) => s.navigate);
  const user = useAuth((s) => s.user);
  const [page, setPage] = useState(1);
  const { data, isLoading, isError, refetch } = useOrders(Boolean(user), page);
  const orders = data?.items;
  const total = data?.total ?? 0;
  const pageSize = data?.page_size ?? 20;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
      <h1 className="mb-8 text-4xl font-bold tracking-tight sm:text-5xl">Мои заказы</h1>

      {!user ? (
        <div className="flex flex-col items-center rounded-3xl border border-border bg-surface px-6 py-14 text-center">
          <span className="mb-4 grid h-14 w-14 place-items-center rounded-full bg-surface-2 text-muted">
            <Icon name="user" size={26} />
          </span>
          <p className="font-medium">Войдите, чтобы видеть заказы</p>
          <button onClick={() => openModal("auth", "login")} className="btn btn-primary mt-5 h-11 px-6">
            Войти
          </button>
        </div>
      ) : isLoading ? (
        <ul className="flex flex-col gap-4">
          {[0, 1, 2].map((i) => (
            <li key={i} className="h-40 animate-pulse rounded-2xl bg-surface-2" />
          ))}
        </ul>
      ) : isError ? (
        <div className="rounded-3xl border border-border px-6 py-14 text-center">
          <p className="text-muted">Не удалось загрузить заказы.</p>
          <button onClick={() => refetch()} className="btn btn-secondary mt-4 h-11">
            Повторить
          </button>
        </div>
      ) : !orders || orders.length === 0 ? (
        <div className="flex flex-col items-center rounded-3xl border border-border bg-surface px-6 py-14 text-center">
          <span className="mb-4 grid h-16 w-16 place-items-center rounded-full bg-surface-2 text-muted">
            <Icon name="clock" size={30} />
          </span>
          <p className="font-medium">Заказов пока нет</p>
          <p className="mt-1 text-sm text-muted">Оформленный заказ появится здесь вместе со статусом.</p>
          <button onClick={() => navigate({ name: "menu" })} className="btn btn-primary mt-5 h-11 px-6">
            Открыть меню
          </button>
        </div>
      ) : (
        <ul className="flex flex-col gap-5">
          {orders.map((order) => {
            const status = ORDER_STATUS[order.status];
            return (
              <li key={order.id} className="rounded-2xl border border-border bg-surface p-5 shadow-sm sm:p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="font-display text-xl font-semibold">Заказ №{order.id}</p>
                    <p className="mt-0.5 text-sm text-muted">{formatOrderDate(order.created_at)}</p>
                  </div>
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ${status.cls}`}
                  >
                    <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-current" />
                    {status.label}
                  </span>
                </div>

                <ul className="mt-5 flex flex-col gap-2 text-sm">
                  {order.items.map((i) => (
                    <li key={i.product_id} className="flex items-baseline justify-between gap-4">
                      <span>
                        {i.name}
                        {i.quantity > 1 && <span className="tnum text-muted"> × {i.quantity}</span>}
                      </span>
                      <span className="tnum shrink-0 text-muted">{formatPrice(i.line_total)}</span>
                    </li>
                  ))}
                </ul>

                <div className="mt-5 flex flex-wrap items-end justify-between gap-3 border-t border-border pt-4">
                  <span className="flex min-w-0 items-start gap-1.5 text-sm text-muted">
                    <Icon name="pin" size={15} className="mt-0.5 shrink-0" />
                    <span className="min-w-0">{order.address}</span>
                  </span>
                  <span className="tnum text-lg font-semibold">{formatPrice(order.total)}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {total > pageSize && (
        <nav aria-label="Страницы заказов" className="mt-8 flex items-center justify-between text-sm text-muted">
          <span>Всего заказов: {total}</span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="icon-btn border border-border disabled:opacity-40"
              aria-label="Предыдущая страница"
            >
              <Icon name="arrowLeft" size={16} />
            </button>
            <span className="tnum min-w-12 text-center">
              {page} из {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="icon-btn border border-border disabled:opacity-40"
              aria-label="Следующая страница"
            >
              <Icon name="arrowRight" size={16} />
            </button>
          </div>
        </nav>
      )}
    </div>
  );
}

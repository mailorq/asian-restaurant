import { useState } from "react";
import { Modal } from "../Modal";
import { Icon } from "../Icon";
import { useToast } from "../../stores/toast";
import { useAuth } from "../../stores/auth";
import {
  useCustomerOrders,
  useEmployeeUsers,
  useSetRole,
  useUserDetail,
  type CustomerOrderPreview,
  type CustomerOrderScope,
  type CustomerOrderSort,
  type EmployeeUser,
  type StaffRole,
} from "../../api/employee";
import { ORDER_STATUS, formatOrderDate } from "../../api/orders";
import { formatPrice } from "../../lib/menu";
import { formatUaPhone } from "../../lib/phone";

const ROLE_LABEL: Record<StaffRole, string> = {
  restaurant_operator: "Оператор",
  restaurant_manager: "Менеджер",
};

const ROLE_CHOICES: { value: StaffRole | null; label: string }[] = [
  { value: "restaurant_operator", label: "Оператор" },
  { value: "restaurant_manager", label: "Менеджер" },
  { value: null, label: "Без роли" },
];

const SORTS: { value: CustomerOrderSort; label: string }[] = [
  { value: "created_at_desc", label: "Сначала новые" },
  { value: "created_at_asc", label: "Сначала старые" },
];

const SCOPES: { value: CustomerOrderScope; label: string }[] = [
  { value: "all", label: "Все" },
  { value: "active", label: "Активные" },
  { value: "history", label: "Завершенные" },
];

function OrderRow({ order }: { order: CustomerOrderPreview }) {
  const status = ORDER_STATUS[order.status];
  return (
    <li className="rounded-xl border border-border p-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">Заказ №{order.id}</span>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${status.cls}`}>
          {status.label}
        </span>
      </div>
      <p className="mt-1 text-xs text-muted">{formatOrderDate(order.created_at)}</p>
      <p className="tnum mt-1 text-right text-sm font-semibold">{formatPrice(order.total)}</p>
    </li>
  );
}

function CustomerOrders({ userId, total }: { userId: number; total: number }) {
  const [page, setPage] = useState(1);
  const [scope, setScope] = useState<CustomerOrderScope>("all");
  const [sort, setSort] = useState<CustomerOrderSort>("created_at_desc");
  const { data, isLoading, isError, refetch } = useCustomerOrders(userId, page, scope, sort);
  const pageSize = data?.page_size ?? 20;
  const totalPages = Math.max(1, Math.ceil((data?.total ?? total) / pageSize));

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {SCOPES.map((option) => (
          <button
            key={option.value}
            onClick={() => {
              setScope(option.value);
              setPage(1);
            }}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              scope === option.value
                ? "bg-primary text-primary-contrast"
                : "border border-border text-muted hover:text-text"
            }`}
          >
            {option.label}
          </button>
        ))}
        <select
          value={sort}
          onChange={(e) => {
            setSort(e.target.value as CustomerOrderSort);
            setPage(1);
          }}
          aria-label="Сортировка"
          className="ml-auto rounded-full border border-border bg-transparent px-3 py-1 text-xs text-muted"
        >
          {SORTS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      {isError ? (
        <div className="rounded-xl border border-border py-8 text-center">
          <p className="text-sm text-muted">Не удалось загрузить историю.</p>
          <button onClick={() => refetch()} className="mt-2 text-sm font-medium text-accent hover:underline">
            Повторить
          </button>
        </div>
      ) : isLoading || !data ? (
        <div className="h-24 animate-pulse rounded-xl bg-surface-2" />
      ) : data.items.length === 0 ? (
        <p className="text-sm text-muted">Заказов нет</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {data.items.map((o) => (
            <OrderRow key={o.id} order={o} />
          ))}
        </ul>
      )}
      {(data?.total ?? 0) > pageSize && (
        <div className="mt-4 flex items-center justify-between text-sm text-muted">
          <span>Всего: {data?.total}</span>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="grid h-8 w-8 place-items-center rounded-full border border-border hover:text-text disabled:opacity-40"
              aria-label="Назад"
            >
              <Icon name="arrowLeft" size={14} />
            </button>
            <span className="tnum">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="grid h-8 w-8 place-items-center rounded-full border border-border hover:text-text disabled:opacity-40"
              aria-label="Вперед"
            >
              <Icon name="arrowRight" size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function UserDetailModal({ userId, onClose }: { userId: number; onClose: () => void }) {
  const { data, isLoading, isError, refetch } = useUserDetail(userId);
  const [showAll, setShowAll] = useState(false);
  return (
    <Modal title={data ? data.name || data.username : "Пользователь"} onClose={onClose}>
      {isError ? (
        <div className="rounded-xl border border-border py-10 text-center">
          <p className="text-sm text-muted">Не удалось загрузить клиента.</p>
          <button onClick={() => refetch()} className="mt-2 text-sm font-medium text-accent hover:underline">
            Повторить
          </button>
        </div>
      ) : isLoading || !data ? (
        <div className="h-40 animate-pulse rounded-xl bg-surface-2" />
      ) : (
        <div>
          <p className="tnum text-sm text-muted">
            {data.phone ? formatUaPhone(data.phone) : "Телефон не указан"}
          </p>
          <div className="mb-3 mt-4 flex items-center justify-between">
            <p className="text-sm font-semibold uppercase tracking-wide text-muted">
              Заказы ({data.orders_total})
            </p>
            {data.orders_total > data.orders_preview.length && (
              <button
                onClick={() => setShowAll((v) => !v)}
                className="text-sm font-medium text-accent hover:underline"
              >
                {showAll ? "Свернуть" : "Все заказы"}
              </button>
            )}
          </div>
          {data.orders_total === 0 ? (
            <p className="text-sm text-muted">Заказов нет</p>
          ) : showAll ? (
            <CustomerOrders userId={data.id} total={data.orders_total} />
          ) : (
            <ul className="flex flex-col gap-2">
              {data.orders_preview.map((o) => (
                <OrderRow key={o.id} order={o} />
              ))}
            </ul>
          )}
        </div>
      )}
    </Modal>
  );
}

export function EmployeeUsers() {
  const me = useAuth((s) => s.user);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [detailId, setDetailId] = useState<number | null>(null);
  const { data, isLoading, isError, refetch } = useEmployeeUsers(search, page);
  const setRole = useSetRole();
  const notify = useToast((s) => s.notify);

  const total = data?.total ?? 0;
  const pageSize = data?.page_size ?? 20;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  function onSearch(value: string) {
    setSearch(value);
    setPage(1);
  }

  function assignRole(u: EmployeeUser, role: StaffRole | null) {
    if (role === u.staff_role) return;
    if (role === null && !confirm(`Отозвать роль у ${u.name || u.username}?`)) return;
    setRole.mutate(
      { userId: u.id, role },
      {
        onSuccess: () =>
          notify(
            role
              ? `${ROLE_LABEL[role]}: ${u.name || u.username}`
              : `Роль отозвана: ${u.name || u.username}`,
          ),
        onError: (e) => notify(e instanceof Error ? e.message : "Не удалось изменить роль", "error"),
      },
    );
  }

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 rounded-xl border border-border bg-surface-2 px-3.5 transition-[border-color,box-shadow] focus-within:border-accent focus-within:shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_22%,transparent)]">
        <Icon name="search" size={18} className="text-muted" />
        <input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Поиск по имени или телефону"
          className="h-11 w-full bg-transparent focus:outline-none"
        />
      </div>

      {isLoading ? (
        <ul className="flex flex-col gap-3">
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="h-16 animate-pulse rounded-2xl bg-surface-2" />
          ))}
        </ul>
      ) : isError ? (
        <div className="rounded-2xl border border-border py-12 text-center">
          <p className="text-sm text-muted">Не удалось загрузить пользователей.</p>
          <button onClick={() => refetch()} className="mt-3 font-medium text-accent hover:underline">
            Повторить
          </button>
        </div>
      ) : !data || data.items.length === 0 ? (
        <p className="rounded-2xl border border-border py-12 text-center text-sm text-muted">
          Никого не найдено
        </p>
      ) : (
        <>
          <ul className="flex flex-col gap-2">
            {data.items.map((u) => (
              <li
                key={u.id}
                className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-border p-4"
              >
                <button
                  onClick={() => setDetailId(u.id)}
                  className="min-w-0 flex-1 text-left"
                >
                  <p className="truncate font-medium">
                    {u.name || u.username}
                    {(u.is_superuser || u.staff_role) && (
                      <span className="ml-2 rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent">
                        {u.is_superuser ? "суперпользователь" : ROLE_LABEL[u.staff_role!]}
                      </span>
                    )}
                  </p>
                  <p className="tnum text-xs text-muted">
                    {u.phone ? formatUaPhone(u.phone) : u.username}
                  </p>
                </button>
                <span className="text-xs text-muted">
                  активных заказов: <span className="tnum font-semibold text-text">{u.active_orders_count}</span>
                </span>
                {me?.is_superuser && !u.is_superuser && (
                  <div className="flex flex-wrap gap-1.5">
                    {ROLE_CHOICES.map((choice) => (
                      <button
                        key={choice.value ?? "none"}
                        onClick={() => assignRole(u, choice.value)}
                        disabled={setRole.isPending}
                        className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
                          u.staff_role === choice.value
                            ? "bg-primary text-primary-contrast"
                            : choice.value === null
                              ? "border border-danger/40 text-danger hover:bg-danger/10"
                              : "border border-border text-muted hover:border-accent hover:text-accent"
                        }`}
                      >
                        {choice.label}
                      </button>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>

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
        </>
      )}

      {detailId !== null && <UserDetailModal userId={detailId} onClose={() => setDetailId(null)} />}
    </div>
  );
}

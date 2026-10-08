import { useState } from "react";
import { Icon } from "../Icon";
import { useToast } from "../../stores/toast";
import { useAdjustStock, useInventory, type InventoryItem } from "../../api/employee";
import { CATEGORY_LABEL_ONE } from "../../lib/menu";

function InventoryRow({ item }: { item: InventoryItem }) {
  const adjust = useAdjustStock();
  const notify = useToast((s) => s.notify);
  const [editing, setEditing] = useState(false);
  const [qty, setQty] = useState(item.stock_quantity);
  const [reason, setReason] = useState("");

  function save() {
    if (reason.trim().length < 2) {
      notify("Укажите причину изменения", "error");
      return;
    }
    adjust.mutate(
      { productId: item.id, new_quantity: qty, reason: reason.trim(), expected_version: item.version },
      {
        onSuccess: () => {
          notify(`${item.name}: остаток ${qty}`);
          setEditing(false);
          setReason("");
        },
        onError: (e) => notify(e instanceof Error ? e.message : "Не удалось изменить остаток", "error"),
      },
    );
  }

  return (
    <li className="rounded-2xl border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-medium">
            {item.name}
            {!item.is_active && <span className="ml-2 text-xs text-danger">неактивен</span>}
          </p>
          <p className="text-xs text-muted">{CATEGORY_LABEL_ONE[item.category]}</p>
        </div>
        <div className="flex items-center gap-3">
          <span
            className={`tnum rounded-full px-3 py-1 text-sm font-semibold ${
              item.stock_quantity === 0
                ? "bg-danger/15 text-danger"
                : item.stock_quantity < 5
                  ? "bg-amber-500/15 text-amber-600 dark:text-amber-400"
                  : "bg-surface-2 text-text"
            }`}
          >
            {item.stock_quantity} шт.
          </span>
          {!editing && (
            <button
              onClick={() => {
                setQty(item.stock_quantity);
                setEditing(true);
              }}
              className="btn btn-secondary h-9 min-h-9 px-4 text-sm text-muted"
            >
              Изменить
            </button>
          )}
        </div>
      </div>

      {editing && (
        <div className="mt-3 flex flex-wrap items-end gap-2 border-t border-border pt-3">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">Новый остаток</span>
            <input
              type="number"
              min={0}
              value={qty}
              onChange={(e) => setQty(Math.max(0, Number(e.target.value)))}
              className="input tnum h-10 w-28"
            />
          </label>
          <label className="block flex-1">
            <span className="mb-1 block text-xs font-medium text-muted">Причина</span>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="поставка, инвентаризация, списание…"
              className="input h-10"
            />
          </label>
          <button
            onClick={save}
            disabled={adjust.isPending}
            className="btn btn-primary h-10 min-h-10 px-4 text-sm"
          >
            Сохранить
          </button>
          <button
            onClick={() => setEditing(false)}
            className="btn btn-ghost h-10 min-h-10 px-3 text-sm"
          >
            Отмена
          </button>
        </div>
      )}
    </li>
  );
}

export function EmployeeInventory() {
  const [search, setSearch] = useState("");
  const { data: items, isLoading, isError, refetch } = useInventory(search);

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 rounded-xl border border-border bg-surface-2 px-3.5 transition-[border-color,box-shadow] focus-within:border-accent focus-within:shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_22%,transparent)]">
        <Icon name="search" size={18} className="text-muted" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Поиск по названию или коду"
          className="h-11 w-full bg-transparent focus:outline-none"
        />
      </div>

      {isLoading ? (
        <ul className="flex flex-col gap-3">
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="h-20 animate-pulse rounded-2xl bg-surface-2" />
          ))}
        </ul>
      ) : isError ? (
        <div className="rounded-2xl border border-border py-12 text-center">
          <p className="text-sm text-muted">Не удалось загрузить каталог.</p>
          <button onClick={() => refetch()} className="mt-3 font-medium text-accent hover:underline">
            Повторить
          </button>
        </div>
      ) : !items || items.length === 0 ? (
        <p className="rounded-2xl border border-border py-12 text-center text-sm text-muted">
          Ничего не найдено
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => (
            <InventoryRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}

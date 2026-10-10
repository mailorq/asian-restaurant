import { useId, useMemo, useState } from "react";
import { Icon } from "./Icon";
import { ProductThumb } from "./ProductThumb";
import { useUI } from "../stores/ui";
import { useProducts } from "../api/menu";
import { CATEGORY_LABEL_ONE, formatPrice } from "../lib/menu";

// a combobox: focus stays in the field, the arrows move through the found dishes and enter opens the
// highlighted one, which is the first until the arrows choose another
export function SearchBox() {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const navigate = useUI((s) => s.navigate);
  const listId = useId();
  const { data: products } = useProducts();

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return (products ?? []).filter((p) => p.name.toLowerCase().includes(q)).slice(0, 5);
  }, [query, products]);
  const shown = open && results.length > 0;
  const current = Math.min(active, results.length - 1);

  function pick(id: number) {
    setQuery("");
    setOpen(false);
    navigate({ name: "product", id });
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!results.length) return;
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(shown ? (current + step + results.length) % results.length : 0);
      setOpen(true);
    } else if (e.key === "Enter" && shown) {
      e.preventDefault();
      pick(results[current].id);
    } else if (e.key === "Escape" && shown) {
      e.preventDefault();
      setOpen(false);
    }
  }

  return (
    <div className="relative w-full max-w-xs">
      <Icon
        name="search"
        size={18}
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
      />
      <input
        role="combobox"
        aria-label="Поиск по меню"
        aria-expanded={shown}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={shown ? `${listId}-${current}` : undefined}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={onKeyDown}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        placeholder="Поиск по меню"
        className="h-10 w-full rounded-full border border-border bg-surface-2 pl-9 pr-4 text-sm text-text placeholder:text-muted focus:border-primary focus:outline-none"
      />

      <div
        id={listId}
        role="listbox"
        aria-label="Найденные блюда"
        hidden={!shown}
        className="anim-fade-up absolute left-0 right-0 top-12 z-50 overflow-hidden rounded-2xl border border-border bg-surface shadow-xl"
      >
        {shown &&
          results.map((p, i) => (
            <div
              key={p.id}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === current}
              // the field keeps focus, so the list stays open until the click lands
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(p.id)}
              className={`flex w-full cursor-pointer items-center gap-3 px-3 py-2.5 text-left transition-colors ${
                i === current ? "bg-surface-2" : ""
              }`}
            >
              <ProductThumb image={p.image} category={p.category} name="" className="h-11 w-11 shrink-0 rounded-lg" iconSize={22} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{p.name}</span>
                <span className="block text-xs text-muted">{CATEGORY_LABEL_ONE[p.category]}</span>
              </span>
              <span className="tnum text-sm font-semibold text-accent">{formatPrice(p.price)}</span>
            </div>
          ))}
      </div>
    </div>
  );
}

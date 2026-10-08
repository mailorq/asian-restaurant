import { useState } from "react";
import { ProductCard } from "../components/ProductCard";
import { useProducts } from "../api/menu";
import { CATEGORY_LABELS, type Category, type Product } from "../lib/menu";
import { plural } from "../lib/format";

type Filter = "all" | Category;

const CATEGORIES: Category[] = ["dish", "drink", "dessert"];

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "Все" },
  ...CATEGORIES.map((value) => ({ value, label: CATEGORY_LABELS[value] })),
];

function Grid({ products }: { products: Product[] }) {
  return (
    <div className="grid grid-cols-1 gap-x-6 gap-y-12 sm:grid-cols-2 lg:grid-cols-3">
      {products.map((product) => (
        <ProductCard key={product.id} product={product} />
      ))}
    </div>
  );
}

export function MenuPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const { data, isLoading, isError, refetch } = useProducts();
  const all = data ?? [];
  const count = (value: Filter) => (value === "all" ? all.length : all.filter((p) => p.category === value).length);
  // the whole menu reads as its sections; one section is shown on its own
  const sections = (filter === "all" ? CATEGORIES : [filter])
    .map((category) => ({ category, products: all.filter((p) => p.category === category) }))
    .filter((section) => section.products.length > 0);

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 sm:py-14">
      <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">Меню</h1>

      <div className="sticky top-16 z-30 -mx-4 mb-10 mt-6 bg-bg/90 px-4 py-3 backdrop-blur-md sm:mx-0 sm:px-0">
        <div role="tablist" aria-label="Разделы меню" className="flex gap-2 overflow-x-auto">
          {FILTERS.map((f) => {
            const active = filter === f.value;
            return (
              <button
                key={f.value}
                role="tab"
                aria-selected={active}
                onClick={() => setFilter(f.value)}
                className={`btn h-10 min-h-10 shrink-0 px-4 text-sm ${active ? "btn-primary" : "btn-secondary text-muted"}`}
              >
                {f.label}
                {!isLoading && <span className={`tnum text-xs ${active ? "opacity-70" : "text-muted"}`}>{count(f.value)}</span>}
              </button>
            );
          })}
        </div>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-1 gap-x-6 gap-y-12 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i}>
              <div className="aspect-[4/3] animate-pulse rounded-2xl bg-surface-2" />
              <div className="mt-4 h-5 w-2/3 animate-pulse rounded bg-surface-2" />
              <div className="mt-2 h-4 w-full animate-pulse rounded bg-surface-2" />
            </div>
          ))}
        </div>
      ) : isError ? (
        <div className="py-16 text-center">
          <p className="text-muted">Не удалось загрузить меню.</p>
          <button onClick={() => refetch()} className="btn btn-secondary mt-4">
            Попробовать снова
          </button>
        </div>
      ) : sections.length === 0 ? (
        <p className="py-16 text-center text-muted">В этом разделе пока ничего нет.</p>
      ) : (
        <div className="space-y-16">
          {sections.map(({ category, products }) => (
            <section key={category} aria-labelledby={filter === "all" ? `section-${category}` : undefined}>
              {filter === "all" && (
                <div className="mb-8 flex items-baseline gap-3 border-b border-border pb-3">
                  <h2 id={`section-${category}`} className="text-2xl font-bold sm:text-3xl">
                    {CATEGORY_LABELS[category]}
                  </h2>
                  <span className="tnum text-sm text-muted">
                    {products.length} {plural(products.length, ["позиция", "позиции", "позиций"])}
                  </span>
                </div>
              )}
              <Grid products={products} />
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

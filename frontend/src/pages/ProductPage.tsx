import { useEffect, useState } from "react";
import { ProductThumb } from "../components/ProductThumb";
import { ProductCard } from "../components/ProductCard";
import { Icon } from "../components/Icon";
import { useUI } from "../stores/ui";
import { useProduct, useProducts } from "../api/menu";
import { useAddItem } from "../api/cart";
import { CATEGORY_LABEL_ONE, formatPrice } from "../lib/menu";

export function ProductPage({ id }: { id: number }) {
  const { data: product, isLoading, isError } = useProduct(id);
  const { data: all } = useProducts();
  const addItem = useAddItem();
  const navigate = useUI((s) => s.navigate);
  const [qty, setQty] = useState(1);
  const [added, setAdded] = useState(false);

  useEffect(() => {
    if (!added) return;
    const timer = setTimeout(() => setAdded(false), 1600);
    return () => clearTimeout(timer);
  }, [added]);

  if (isLoading) {
    return (
      <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
        <div className="grid gap-10 lg:grid-cols-2">
          <div className="aspect-square animate-pulse rounded-3xl bg-surface-2" />
          <div className="space-y-4">
            <div className="h-10 w-2/3 animate-pulse rounded bg-surface-2" />
            <div className="h-8 w-1/3 animate-pulse rounded bg-surface-2" />
            <div className="h-24 animate-pulse rounded bg-surface-2" />
          </div>
        </div>
      </div>
    );
  }

  if (isError || !product) {
    return (
      <div className="mx-auto max-w-6xl px-4 py-24 text-center sm:px-6">
        <p className="text-lg font-medium">Позиция не найдена</p>
        <button onClick={() => navigate({ name: "menu" })} className="mt-4 text-accent hover:underline">
          Вернуться в меню
        </button>
      </div>
    );
  }

  const related = (all ?? [])
    .filter((p) => p.category === product.category && p.id !== product.id)
    .slice(0, 3);
  const maxQty = Math.max(1, product.stock);
  const qtyCapped = Math.min(qty, maxQty);

  function addToCart() {
    if (!product!.available || addItem.isPending) return;
    addItem.mutate({ productId: product!.id, quantity: qtyCapped }, { onSuccess: () => setAdded(true) });
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
      <button onClick={() => navigate({ name: "menu" })} className="btn btn-ghost -ml-3 mb-6 h-10 min-h-10 px-3 text-sm">
        <Icon name="arrowLeft" size={18} /> Назад в меню
      </button>

      <div className="grid gap-10 lg:grid-cols-2 lg:gap-14">
        <ProductThumb
          category={product.category}
          name={product.name}
          image={product.image}
          priority
          className="aspect-square rounded-3xl shadow-lg ring-1 ring-border"
          iconSize={120}
        />

        <div className="flex flex-col lg:py-4">
          <p className="text-sm text-muted">
            {CATEGORY_LABEL_ONE[product.category]}
            {!product.available && <span className="ml-2 font-medium text-danger">нет в наличии</span>}
          </p>
          <h1 className="mt-2 text-4xl font-bold leading-[1.08] tracking-tight sm:text-5xl">{product.name}</h1>
          <p className="tnum mt-5 text-3xl font-semibold text-accent">{formatPrice(product.price)}</p>
          <p className="mt-5 max-w-prose text-lg leading-relaxed text-muted">{product.description}</p>

          <div className="mt-8">
            <h2 className="font-sans text-sm font-semibold text-text">Состав</h2>
            <div className="mt-3 flex flex-wrap gap-2">
              {product.ingredients.map((ing) => (
                <span key={ing} className="chip">
                  {ing}
                </span>
              ))}
            </div>
            {product.allergens.length > 0 && (
              <p className="mt-4 text-sm text-muted">Аллергены: {product.allergens.join(", ")}</p>
            )}
          </div>

          <div className="mt-10 flex flex-wrap items-center gap-3 border-t border-border pt-6 sm:flex-nowrap">
            <div className="flex h-12 items-center gap-1 rounded-full border border-border px-1.5">
              <button
                onClick={() => setQty((q) => Math.max(1, q - 1))}
                aria-label="Меньше"
                disabled={qtyCapped <= 1}
                className="icon-btn h-9 w-9"
              >
                <Icon name="minus" size={17} strokeWidth={2} />
              </button>
              <span className="tnum w-8 text-center font-semibold" aria-live="polite">
                {qtyCapped}
              </span>
              <button
                onClick={() => setQty((q) => Math.min(maxQty, q + 1))}
                aria-label="Больше"
                disabled={qtyCapped >= maxQty}
                className="icon-btn h-9 w-9 disabled:opacity-40"
              >
                <Icon name="plus" size={17} strokeWidth={2} />
              </button>
            </div>
            {product.available ? (
              <button onClick={addToCart} disabled={addItem.isPending} className="btn btn-primary h-12 flex-1 text-base">
                <Icon name={added ? "check" : "cart"} size={19} />
                {added ? "Добавлено в корзину" : `Добавить за ${formatPrice(product.price * qtyCapped)}`}
              </button>
            ) : (
              <span className="flex h-12 flex-1 items-center justify-center rounded-full bg-surface-2 font-medium text-muted">
                Нет в наличии
              </span>
            )}
          </div>
        </div>
      </div>

      {related.length > 0 && (
        <section className="mt-20 border-t border-border pt-12">
          <h2 className="mb-8 text-3xl font-bold tracking-tight">Похожие позиции</h2>
          <div className="grid grid-cols-1 gap-x-6 gap-y-12 sm:grid-cols-2 lg:grid-cols-3">
            {related.map((p) => (
              <ProductCard key={p.id} product={p} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

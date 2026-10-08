import { useEffect, useState } from "react";
import { ProductThumb } from "./ProductThumb";
import { Icon } from "./Icon";
import { useAddItem } from "../api/cart";
import { useUI } from "../stores/ui";
import { formatPrice, type Product } from "../lib/menu";

export function ProductCard({ product }: { product: Product }) {
  const addItem = useAddItem();
  const navigate = useUI((s) => s.navigate);
  const [added, setAdded] = useState(false);

  useEffect(() => {
    if (!added) return;
    const timer = setTimeout(() => setAdded(false), 1400);
    return () => clearTimeout(timer);
  }, [added]);

  function addToCart() {
    if (!product.available || addItem.isPending) return;
    addItem.mutate({ productId: product.id }, { onSuccess: () => setAdded(true) });
  }

  return (
    <article className="group relative flex flex-col">
      <div className="relative overflow-hidden rounded-2xl shadow-sm ring-1 ring-border transition-[box-shadow] duration-300 group-hover:shadow-md group-hover:ring-accent/45">
        <ProductThumb
          category={product.category}
          name={product.name}
          image={product.image}
          className={`aspect-[4/3] ${product.available ? "" : "opacity-55 grayscale-[35%]"}`}
          imageClassName="transition-transform duration-[var(--dur-slow)] ease-[var(--ease-out)] group-hover:scale-[1.04]"
        />
        {!product.available && (
          <span className="absolute left-3 top-3 rounded-full bg-bg/90 px-3 py-1 text-xs font-medium text-muted">
            Нет в наличии
          </span>
        )}
      </div>

      <div className="mt-4 flex items-baseline justify-between gap-4">
        <h3 className="font-display text-xl font-semibold leading-snug">
          {/* the title opens the dish; its hit area covers the whole card, under the cart button */}
          <button
            onClick={() => navigate({ name: "product", id: product.id })}
            className="text-left after:absolute after:inset-0 after:rounded-2xl focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-4 focus-visible:after:outline-accent"
          >
            {product.name}
          </button>
        </h3>
        <span className="tnum shrink-0 text-lg font-semibold">{formatPrice(product.price)}</span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-sm leading-relaxed text-muted">{product.description}</p>

      <div className="relative z-10 mt-4">
        {product.available ? (
          <button
            onClick={addToCart}
            disabled={addItem.isPending}
            aria-label={`Добавить «${product.name}» в корзину`}
            className={`btn h-10 min-h-10 px-4 text-sm ${added ? "btn-primary" : "btn-secondary"}`}
          >
            <Icon name={added ? "check" : "plus"} size={16} strokeWidth={2.2} />
            {added ? "Добавлено" : "В корзину"}
          </button>
        ) : (
          <span className="inline-flex h-10 items-center text-sm text-muted">Скоро вернется в меню</span>
        )}
        <span className="sr-only" aria-live="polite">
          {added ? `«${product.name}» в корзине` : ""}
        </span>
      </div>
    </article>
  );
}

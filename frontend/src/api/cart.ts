import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { api, ApiError } from "./client";
import { useToast } from "../stores/toast";
import type { Category } from "../lib/menu";

export interface CartLine {
  product_id: number;
  name: string;
  category: Category;
  price: number;
  quantity: number;
  image: string | null;
  available: boolean;
}

export interface CartAdjustment {
  product_id: number;
  name: string;
  from_qty: number;
  to_qty: number;
  reason: string;
}

export interface CartRemoved {
  product_id: number;
  name: string;
  reason: string;
}

export interface Cart {
  version: number;
  items: CartLine[];
  total: number;
  count: number;
  adjustments: CartAdjustment[];
  removed_items: CartRemoved[];
  // an order of the previous release this cart may still hold lines of, until its owner confirms it
  review_order?: number | null;
}

const CART_KEY = ["cart"] as const;

type Notify = (message: string, tone?: "success" | "error") => void;

// thrown when a version conflict could not be resolved automatically; carries
// the server's fresh cart so the ui can resync and ask the user to retry
class CartConflictError extends Error {
  cart: Cart;
  constructor(cart: Cart) {
    super("cart_version_conflict");
    this.cart = cart;
  }
}

function conflictCart(e: unknown): Cart | null {
  if (e instanceof ApiError && e.status === 409) {
    return (e.body as { cart?: Cart } | null)?.cart ?? null;
  }
  return null;
}

function surface(cart: Cart, notify: Notify): void {
  for (const a of cart.adjustments) {
    notify(`Количество «${a.name}» ограничено до ${a.to_qty} шт. из-за остатка`, "error");
  }
  for (const r of cart.removed_items) {
    notify(
      r.reason === "out_of_stock"
        ? `«${r.name}» закончился и удалён из корзины`
        : `«${r.name}» больше недоступен и удалён из корзины`,
      "error",
    );
  }
}

// Send the write with the last-seen version. Only a relative add may auto-retry
// once against the fresh version; absolute set/remove/clear must not, since a
// stale overwrite could clobber a change from another tab — instead we surface
// the fresh cart (CartConflictError) and let the user repeat the action.
async function writeWithVersion(
  qc: QueryClient,
  call: (expected: number | undefined) => Promise<Cart>,
  retry: boolean,
): Promise<Cart> {
  const current = qc.getQueryData<Cart>(CART_KEY);
  try {
    return await call(current?.version);
  } catch (e) {
    const fresh = conflictCart(e);
    if (!fresh) throw e;
    if (retry) {
      try {
        return await call(fresh.version);
      } catch (e2) {
        throw new CartConflictError(conflictCart(e2) ?? fresh);
      }
    }
    throw new CartConflictError(fresh);
  }
}

export function useCartQuery() {
  return useQuery({ queryKey: CART_KEY, queryFn: () => api<Cart>("/cart"), staleTime: 30_000 });
}

function useCartMutation<V>(run: (qc: QueryClient, vars: V) => Promise<Cart>) {
  const qc = useQueryClient();
  const notify = useToast((s) => s.notify);
  return useMutation({
    mutationFn: (vars: V) => run(qc, vars),
    onSuccess: (cart) => {
      qc.setQueryData(CART_KEY, cart);
      surface(cart, notify);
    },
    onError: (e) => {
      if (e instanceof CartConflictError) {
        qc.setQueryData(CART_KEY, e.cart);
        notify("Корзина изменилась — проверьте её и повторите действие", "error");
      } else {
        notify(e instanceof Error ? e.message : "Не удалось обновить корзину", "error");
      }
    },
  });
}

export function useAddItem() {
  return useCartMutation((qc: QueryClient, vars: { productId: number; quantity?: number }) =>
    writeWithVersion(
      qc,
      (expected) =>
        api<Cart>("/cart/items", {
          method: "POST",
          body: JSON.stringify({
            product_id: vars.productId,
            quantity: vars.quantity ?? 1,
            expected_version: expected,
          }),
        }),
      true,
    ),
  );
}

export function useSetItem() {
  return useCartMutation((qc: QueryClient, vars: { productId: number; quantity: number }) =>
    writeWithVersion(
      qc,
      (expected) =>
        api<Cart>(`/cart/items/${vars.productId}`, {
          method: "PUT",
          body: JSON.stringify({ quantity: vars.quantity, expected_version: expected }),
        }),
      false,
    ),
  );
}

export function useRemoveItem() {
  return useCartMutation((qc: QueryClient, vars: { productId: number }) =>
    writeWithVersion(
      qc,
      (expected) =>
        api<Cart>(
          `/cart/items/${vars.productId}${expected === undefined ? "" : `?expected_version=${expected}`}`,
          { method: "DELETE" },
        ),
      false,
    ),
  );
}

export function useConfirmCart() {
  return useCartMutation<void>((qc: QueryClient) =>
    writeWithVersion(
      qc,
      (expected) =>
        api<Cart>("/cart/review", {
          method: "POST",
          body: JSON.stringify({ expected_version: expected }),
        }),
      false,
    ),
  );
}

export function useClearCart() {
  return useCartMutation<void>((qc: QueryClient) =>
    writeWithVersion(
      qc,
      (expected) =>
        api<Cart>(`/cart${expected === undefined ? "" : `?expected_version=${expected}`}`, {
          method: "DELETE",
        }),
      false,
    ),
  );
}

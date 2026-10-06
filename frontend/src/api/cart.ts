import { useQuery, useQueryClient, type QueryClient, type UseMutationOptions } from "@tanstack/react-query";

import { api, ApiError } from "./client";
import { useToast } from "../stores/toast";
import type { Category } from "../lib/menu";
import { currentSession, useSessionMutation } from "../lib/sessionCache";

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
  const started = currentSession();
  const current = qc.getQueryData<Cart>(CART_KEY);
  try {
    return await call(current?.version);
  } catch (e) {
    const fresh = conflictCart(e);
    if (!fresh) throw e;
    // the retry goes out only in the session the action started in, never on behalf of the next account
    if (retry && started === currentSession()) {
      try {
        return await call(fresh.version);
      } catch (e2) {
        throw new CartConflictError(conflictCart(e2) ?? fresh);
      }
    }
    throw new CartConflictError(fresh);
  }
}

export const cartQuery = { queryKey: CART_KEY, queryFn: () => api<Cart>("/cart"), staleTime: 30_000 };

export function useCartQuery() {
  return useQuery(cartQuery);
}

export function cartMutation<V>(
  qc: QueryClient,
  notify: Notify,
  run: (qc: QueryClient, vars: V) => Promise<Cart>,
): UseMutationOptions<Cart, Error, V, number> {
  return {
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
  };
}

function useCartMutation<V>(run: (qc: QueryClient, vars: V) => Promise<Cart>) {
  const qc = useQueryClient();
  const notify = useToast((s) => s.notify);
  return useSessionMutation(cartMutation(qc, notify, run));
}

export function addItem(qc: QueryClient, vars: { productId: number; quantity?: number }) {
  return writeWithVersion(
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
  );
}

export function setItem(qc: QueryClient, vars: { productId: number; quantity: number }) {
  return writeWithVersion(
    qc,
    (expected) =>
      api<Cart>(`/cart/items/${vars.productId}`, {
        method: "PUT",
        body: JSON.stringify({ quantity: vars.quantity, expected_version: expected }),
      }),
    false,
  );
}

export function removeItem(qc: QueryClient, vars: { productId: number }) {
  return writeWithVersion(
    qc,
    (expected) =>
      api<Cart>(
        `/cart/items/${vars.productId}${expected === undefined ? "" : `?expected_version=${expected}`}`,
        { method: "DELETE" },
      ),
    false,
  );
}

export function confirmCart(qc: QueryClient) {
  return writeWithVersion(
    qc,
    (expected) =>
      api<Cart>("/cart/review", {
        method: "POST",
        body: JSON.stringify({ expected_version: expected }),
      }),
    false,
  );
}

export function clearCart(qc: QueryClient) {
  return writeWithVersion(
    qc,
    (expected) =>
      api<Cart>(`/cart${expected === undefined ? "" : `?expected_version=${expected}`}`, {
        method: "DELETE",
      }),
    false,
  );
}

export function useAddItem() {
  return useCartMutation(addItem);
}

export function useSetItem() {
  return useCartMutation(setItem);
}

export function useRemoveItem() {
  return useCartMutation(removeItem);
}

export function useConfirmCart() {
  return useCartMutation<void>(confirmCart);
}

export function useClearCart() {
  return useCartMutation<void>(clearCart);
}

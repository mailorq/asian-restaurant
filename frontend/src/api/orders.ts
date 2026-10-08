import { useQuery, useQueryClient, type QueryClient, type UseMutationOptions } from "@tanstack/react-query";

import { ApiError, api } from "./client";
import type { Cart } from "./cart";
import { useSessionMutation } from "../lib/sessionCache";

export type OrderStatus =
  | "created"
  | "confirmed"
  | "preparing"
  | "delivering"
  | "delivered"
  | "cancelled";

export interface OrderItem {
  product_id: number;
  name: string;
  unit_price: number;
  quantity: number;
  line_total: number;
}

export interface Order {
  id: number;
  status: OrderStatus;
  payment_method: "cash" | "card";
  total: number;
  phone: string;
  contact_name: string;
  address: string;
  address_verified: boolean;
  items: OrderItem[];
  created_at: string;
}

export const ORDER_STATUS: Record<OrderStatus, { label: string; cls: string }> = {
  created: { label: "Создан", cls: "bg-accent/15 text-accent" },
  confirmed: { label: "Подтвержден", cls: "bg-accent/15 text-accent" },
  preparing: { label: "Готовится", cls: "bg-accent/15 text-accent" },
  delivering: { label: "В доставке", cls: "bg-sky-500/15 text-sky-600 dark:text-sky-400" },
  delivered: {
    label: "Доставлен",
    cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  },
  cancelled: { label: "Отменен", cls: "bg-danger/15 text-danger" },
};

const dateFmt = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatOrderDate(iso: string): string {
  return dateFmt.format(new Date(iso));
}

const EMPTY_CART: Cart = {
  version: 0,
  items: [],
  total: 0,
  count: 0,
  adjustments: [],
  removed_items: [],
};

export interface LastAddress {
  address: string;
  is_verified: boolean;
}

export interface AddressVerification {
  verified: boolean;
  display_name: string;
  lat: number | null;
  lng: number | null;
}

export interface PagedOrders {
  items: Order[];
  total: number;
  page: number;
  page_size: number;
}

export function useOrders(enabled = true, page = 1, pageSize = 20) {
  return useQuery({
    queryKey: ["orders", page, pageSize],
    queryFn: () => api<PagedOrders>(`/orders?page=${page}&page_size=${pageSize}`),
    enabled,
  });
}

export function useLastAddress(enabled = true) {
  return useQuery({
    queryKey: ["last-address"],
    queryFn: () => api<LastAddress>("/orders/address/last"),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useVerifyAddress() {
  return useSessionMutation({
    mutationFn: (address: string) =>
      api<AddressVerification>("/orders/address/verify", {
        method: "POST",
        body: JSON.stringify({ address }),
      }),
  });
}

interface CheckoutForm {
  address: string;
  payment_method: "cash" | "card";
  recipient_name: string;
  idempotency_key: string;
}

export function checkoutMutation(qc: QueryClient): UseMutationOptions<Order, Error, CheckoutForm, number> {
  return {
    mutationFn: (vars) =>
      api<Order>("/orders/checkout", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    onSuccess: () => {
      qc.setQueryData(["cart"], EMPTY_CART); // checkout clears the cart server-side
      qc.invalidateQueries({ queryKey: ["cart"] });
      qc.invalidateQueries({ queryKey: ["orders"] });
    },
    // an error does not prove that no order was placed, and a conflict means the cart moved on
    onError: (err) => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      if (err instanceof ApiError && err.status === 409) {
        qc.invalidateQueries({ queryKey: ["cart"] });
      }
    },
  };
}

export function useCheckout() {
  return useSessionMutation(checkoutMutation(useQueryClient()));
}

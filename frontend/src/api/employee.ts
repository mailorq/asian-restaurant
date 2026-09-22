import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api } from "./client";
import type { Order, OrderStatus, PagedOrders } from "./orders";
import type { Category } from "../lib/menu";

export interface InventoryItem {
  id: number;
  code: string;
  name: string;
  category: Category;
  stock_quantity: number;
  is_active: boolean;
  version: number;
}

export interface StockAdjustmentRow {
  old_quantity: number;
  new_quantity: number;
  reason: string;
  staff: string | null;
  created_at: string;
}

export interface EmployeeUser {
  id: number;
  username: string;
  name: string;
  phone: string | null;
  is_employee: boolean;
  is_superuser: boolean;
  staff_role: StaffRole | null;
  active_orders_count: number;
  date_joined: string;
}

export interface PagedUsers {
  items: EmployeeUser[];
  total: number;
  page: number;
  page_size: number;
}

export type StaffRole = "restaurant_operator" | "restaurant_manager";

export interface CustomerOrderPreview {
  id: number;
  status: OrderStatus;
  total: number;
  created_at: string;
}

export interface UserDetail {
  id: number;
  username: string;
  name: string;
  phone: string | null;
  is_employee: boolean;
  is_superuser: boolean;
  staff_role: StaffRole | null;
  orders_total: number;
  orders_preview: CustomerOrderPreview[];
}

export interface PagedCustomerOrders {
  items: CustomerOrderPreview[];
  total: number;
  page: number;
  page_size: number;
}

export type CustomerOrderScope = "active" | "history" | "all";
export type CustomerOrderSort = "created_at_desc" | "created_at_asc";

// --- orders ---------------------------------------------------------------
export function useEmployeeOrders(status: OrderStatus | "", page = 1, pageSize = 20) {
  return useQuery({
    queryKey: ["employee", "orders", status || "all", page, pageSize],
    queryFn: () =>
      api<PagedOrders>(
        `/employee/orders?page=${page}&page_size=${pageSize}${status ? `&status=${status}` : ""}`,
      ),
    refetchInterval: 15_000, // polling until SSE (Stage 5)
  });
}

export function useTransitionOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      orderId: number;
      to_status: OrderStatus;
      expected_status: OrderStatus;
      note?: string;
    }) =>
      api<Order>(`/employee/orders/${vars.orderId}/transition`, {
        method: "POST",
        body: JSON.stringify({
          to_status: vars.to_status,
          expected_status: vars.expected_status,
          note: vars.note ?? "",
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["employee", "orders"] }),
  });
}

// --- inventory ------------------------------------------------------------
export function useInventory(search: string) {
  return useQuery({
    queryKey: ["employee", "inventory", search],
    queryFn: () => api<InventoryItem[]>(`/employee/inventory${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  });
}

export function useAdjustStock() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { productId: number; new_quantity: number; reason: string; expected_version: number }) =>
      api<InventoryItem>(`/employee/inventory/${vars.productId}/adjust`, {
        method: "POST",
        body: JSON.stringify({
          new_quantity: vars.new_quantity,
          reason: vars.reason,
          expected_version: vars.expected_version,
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["employee", "inventory"] }),
    // a 409 means the stock moved since the list was loaded: reload it so the next save carries the new version
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: ["employee", "inventory"] });
    },
  });
}

// --- users ----------------------------------------------------------------
export function useEmployeeUsers(search: string, page: number, pageSize = 20) {
  return useQuery({
    queryKey: ["employee", "users", search, page, pageSize],
    queryFn: () =>
      api<PagedUsers>(
        `/employee/users?page=${page}&page_size=${pageSize}${search ? `&search=${encodeURIComponent(search)}` : ""}`,
      ),
  });
}

export function useUserDetail(userId: number | null) {
  return useQuery({
    queryKey: ["employee", "user", userId],
    queryFn: () => api<UserDetail>(`/employee/users/${userId}`),
    enabled: userId !== null,
  });
}

export function useCustomerOrders(
  userId: number | null,
  page: number,
  scope: CustomerOrderScope = "all",
  sort: CustomerOrderSort = "created_at_desc",
  enabled = true,
  pageSize = 20,
) {
  return useQuery({
    queryKey: ["employee", "user", userId, "orders", page, scope, sort, pageSize],
    queryFn: () =>
      api<PagedCustomerOrders>(
        `/employee/users/${userId}/orders?page=${page}&page_size=${pageSize}&scope=${scope}&sort=${sort}`,
      ),
    enabled: enabled && userId !== null,
  });
}

export function useSetRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { userId: number; role: StaffRole | null }) =>
      api<EmployeeUser>(`/employee/users/${vars.userId}/role`, {
        method: "POST",
        body: JSON.stringify({ role: vars.role }),
      }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["employee", "users"] });
      qc.invalidateQueries({ queryKey: ["employee", "user", vars.userId] });
    },
  });
}

import { create } from "zustand";

import { api } from "../api/client";
import { queryClient } from "../lib/queryClient";

// identity changes may trigger a guest->user cart merge server-side; refetch it
function refreshCart() {
  queryClient.invalidateQueries({ queryKey: ["cart"] });
}

export interface CurrentUser {
  id: number;
  phone: string | null;
  name: string;
  is_employee: boolean;
  is_superuser: boolean;
  staff_role: "restaurant_operator" | "restaurant_manager" | null;
  transitions_via_commands: boolean;
}

interface AuthState {
  user: CurrentUser | null;
  ready: boolean;
  setUser: (user: CurrentUser | null) => void;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  ready: false,
  setUser: (user) => {
    set({ user });
    refreshCart();
  },
  refresh: async () => {
    try {
      const user = await api<CurrentUser>("/auth/me");
      set({ user, ready: true });
    } catch {
      set({ user: null, ready: true });
    }
  },
  logout: async () => {
    try {
      await api("/auth/csrf"); // fresh token for the current session
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* clear locally regardless */
    }
    set({ user: null });
    refreshCart();
  },
}));

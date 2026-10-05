import { create } from "zustand";

import { api } from "../api/client";
import { queryClient } from "../lib/queryClient";
import { onIdentityChange } from "../lib/sessionCache";

function identity(user: CurrentUser | null): number | null {
  return user?.id ?? null;
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

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  ready: false,
  setUser: (user) => {
    // a login or recovery also merges any guest cart server-side; dropping the cache refetches it
    onIdentityChange(queryClient, identity(get().user), identity(user));
    set({ user });
  },
  refresh: async () => {
    try {
      const user = await api<CurrentUser>("/auth/me");
      onIdentityChange(queryClient, identity(get().user), identity(user));
      set({ user, ready: true });
    } catch {
      onIdentityChange(queryClient, identity(get().user), null);
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
    onIdentityChange(queryClient, identity(get().user), null);
    set({ user: null });
  },
}));

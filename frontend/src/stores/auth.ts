import { create } from "zustand";

import { ApiError, api } from "../api/client";
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

export const useAuth = create<AuthState>((set, get) => {
  // counts every identity this tab applies: an answer to /auth/me counts only if none came after it was asked
  let applied = 0;

  function apply(user: CurrentUser | null): void {
    // a login or recovery also merges any guest cart server-side; dropping the cache refetches it
    onIdentityChange(queryClient, identity(get().user), identity(user));
    applied += 1;
    set({ user, ready: true });
  }

  return {
    user: null,
    ready: false,
    setUser: apply,
    refresh: async () => {
      const asked = applied;
      try {
        const user = await api<CurrentUser>("/auth/me");
        if (asked === applied) apply(user);
      } catch (e) {
        if (asked !== applied) return;
        // only a refusal says nobody is signed in; a failed request leaves a known account as it was
        const refused = e instanceof ApiError && (e.status === 401 || e.status === 403);
        if (refused || !get().ready) apply(null);
      }
    },
    logout: async () => {
      try {
        await api("/auth/csrf"); // fresh token for the current session
        await api("/auth/logout", { method: "POST" });
      } catch {
        /* clear locally regardless */
      }
      apply(null);
    },
  };
});

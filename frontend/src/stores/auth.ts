import { create } from "zustand";

import { ApiError, api, isAccountChanged } from "../api/client";
import { queryClient } from "../lib/queryClient";
import { currentAccount, onAccountChanged } from "../lib/session";
import { onIdentityChange } from "../lib/sessionCache";
import { useToast } from "./toast";

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

const SWITCHED = "Аккаунт сменился в другой вкладке, данные обновлены";

// tabs share cookies: a login or logout here tells the others to ask who is signed in now. it only
// speeds them up, the server refuses whatever they send for an account the browser no longer has
const tabs = typeof window !== "undefined" && "BroadcastChannel" in window ? new BroadcastChannel("auth") : null;

export const useAuth = create<AuthState>((set) => {
  // counts every identity this tab applies: an answer to /auth/me counts only if none came after it was asked
  let applied = 0;

  function apply(user: CurrentUser | null): void {
    const previous = currentAccount();
    const next = user?.id ?? null;
    onIdentityChange(queryClient, previous, next);
    if (previous !== undefined && previous !== next) useToast.getState().clear();
    applied += 1;
    set({ user, ready: true });
  }

  async function refresh(): Promise<void> {
    const asked = applied;
    try {
      const user = await api<CurrentUser>("/auth/me");
      if (asked === applied) apply(user);
    } catch (e) {
      if (asked !== applied) return;
      // only a refusal says nobody is signed in; a failed request leaves a known account as it was
      const refused = e instanceof ApiError && (e.status === 401 || e.status === 403);
      if (refused || currentAccount() === undefined) apply(null);
    }
  }

  // the browser is signed in as someone else now: nothing of this account is kept, repeated or carried
  // over, and the tab finds out whose it is
  function resync(): void {
    if (currentAccount() === undefined) return;
    onIdentityChange(queryClient, currentAccount(), undefined);
    applied += 1;
    useToast.getState().clear();
    useToast.getState().notify(SWITCHED);
    set({ user: null, ready: false });
    void refresh();
  }

  onAccountChanged(resync);
  if (tabs) {
    tabs.onmessage = async () => {
      const before = currentAccount();
      await refresh();
      if (before !== undefined && currentAccount() !== before) useToast.getState().notify(SWITCHED);
    };
  }

  return {
    user: null,
    ready: false,
    setUser: (user) => {
      apply(user);
      tabs?.postMessage("identity");
    },
    refresh,
    logout: async () => {
      try {
        await api("/auth/csrf"); // fresh token for the current session
        await api("/auth/logout", { method: "POST" });
      } catch (e) {
        // the browser was signed in as someone else, the resync took this tab over
        if (isAccountChanged(e)) return;
      }
      apply(null);
      tabs?.postMessage("identity");
    },
  };
});

import type { QueryClient } from "@tanstack/react-query";

// every cached query belongs to the signed-in account: its orders, its last used address, its cart,
// the screens a staff member sees. when the identity changes - login, logout, register, a password
// reset, a refresh that sees a different account - the whole cache is dropped and its in-flight
// fetches are detached, so the next account never reads the previous one's data and a slow answer to
// the previous account's request cannot land in the new cache. a re-validation of the same account
// keeps its warm cache
export function onIdentityChange(
  qc: QueryClient,
  previousId: number | null,
  nextId: number | null,
): void {
  if (previousId === nextId) {
    return;
  }
  qc.clear();
}

import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";

import { QueryClient, QueryObserver } from "@tanstack/react-query";

register("./resolve-js.mjs", import.meta.url);

const { onIdentityChange } = await import("../.test-build/lib/sessionCache.js");

const ORDERS_KEY = ["orders", 1, 20];
const ADDRESS_KEY = ["last-address"];
const A_PRIVATE = "USER_A_PRIVATE_ADDRESS";

function withPrivateDataOf() {
  const qc = new QueryClient();
  qc.setQueryData(ORDERS_KEY, { items: [{ id: 77, address: A_PRIVATE }], total: 1, page: 1, page_size: 20 });
  qc.setQueryData(ADDRESS_KEY, { address: A_PRIVATE, is_verified: false });
  return qc;
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 10));

test("a re-validation of the same account keeps its warm cache", () => {
  const qc = withPrivateDataOf();
  onIdentityChange(qc, 101, 101);
  assert.ok(qc.getQueryData(ORDERS_KEY));
  assert.ok(qc.getQueryData(ADDRESS_KEY));
});

test("an account change drops the previous account's private cache", () => {
  const qc = withPrivateDataOf();
  onIdentityChange(qc, 101, 202);
  assert.equal(qc.getQueryData(ORDERS_KEY), undefined);
  assert.equal(qc.getQueryData(ADDRESS_KEY), undefined);
});

test("the next account never shows the previous one's orders, even before its own answer arrives", async () => {
  const qc = withPrivateDataOf();
  onIdentityChange(qc, 101, 202);
  // same key and enabled predicate as useOrders(Boolean(user), page); the new account's answer is slow
  const observer = new QueryObserver(qc, { queryKey: ORDERS_KEY, enabled: true, queryFn: () => new Promise(() => {}) });
  const unsubscribe = observer.subscribe(() => {});
  await tick();
  const shown = observer.getCurrentResult();
  assert.notEqual(shown.data?.items?.[0]?.address, A_PRIVATE);
  assert.equal(shown.isLoading, true);
  unsubscribe();
  observer.destroy();
});

test("the last used address is re-fetched for the new account, not served fresh from the previous one", async () => {
  const qc = withPrivateDataOf();
  onIdentityChange(qc, 101, 202);
  let fetches = 0;
  const observer = new QueryObserver(qc, {
    queryKey: ADDRESS_KEY,
    enabled: true,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      fetches += 1;
      return { address: "USER_B_ADDRESS", is_verified: false };
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  await tick();
  assert.equal(fetches, 1);
  assert.equal(observer.getCurrentResult().data?.address, "USER_B_ADDRESS");
  unsubscribe();
  observer.destroy();
});

// control: with the identity boundary deliberately inert (same id), the fresh, long-staleTime address
// of the previous account is served to the next one without a fetch - the exact leak the change closes
test("without clearing on identity change the previous account's address leaks (control)", async () => {
  const qc = withPrivateDataOf();
  onIdentityChange(qc, 101, 101);
  let fetches = 0;
  const observer = new QueryObserver(qc, {
    queryKey: ADDRESS_KEY,
    enabled: true,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      fetches += 1;
      return { address: "USER_B_ADDRESS", is_verified: false };
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  await tick();
  assert.equal(fetches, 0);
  assert.equal(observer.getCurrentResult().data?.address, A_PRIVATE);
  unsubscribe();
  observer.destroy();
});

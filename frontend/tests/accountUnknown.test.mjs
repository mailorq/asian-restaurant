import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";

import { QueryObserver } from "@tanstack/react-query";

register("./resolve-js.mjs", import.meta.url);

// a fresh process: nobody has asked yet who is signed in
const { useAuth } = await import("../.test-build/stores/auth.js");
const { queryClient } = await import("../.test-build/lib/queryClient.js");
const { api } = await import("../.test-build/api/client.js");
const { accountChanged, currentAccount } = await import("../.test-build/lib/session.js");

const A = {
  id: 101,
  phone: null,
  name: "account 101",
  is_employee: false,
  is_superuser: false,
  staff_role: null,
  transitions_via_commands: false,
};
const CART = { version: 1, items: [], total: 0, count: 0, adjustments: [], removed_items: [], review_order: null };
const UNAVAILABLE = () => json(503, { detail: "Сервис временно недоступен" });
const UNREACHABLE = () => {
  throw new TypeError("Failed to fetch");
};

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
const unknownAccount = (e) => e.status === 0 && e.body?.code === "account_unknown";
const state = () => {
  const { user, ready, failed } = useAuth.getState();
  return { user: user?.id ?? null, ready, failed };
};

let routes = new Map();
const sent = [];
globalThis.document = { cookie: "csrftoken=test-token" };
globalThis.fetch = async (url, init = {}) => {
  const call = `${(init.method ?? "GET").toUpperCase()} ${url}`;
  const claimed = init.headers instanceof Headers ? init.headers.get("X-Account") : null;
  sent.push({ call, claimed });
  const answer = routes.get(call);
  if (!answer) throw new Error(`no answer for ${call}`);
  return answer();
};
const requests = (call) => sent.filter((request) => request.call === call);

function serve(extra) {
  routes = new Map([["GET /api/auth/csrf", () => json(200, {})], ...extra]);
  sent.length = 0;
}

test("a first /auth/me the server fails to answer leaves the account unknown and refuses what waits for it", async () => {
  let release;
  const me = new Promise((resolve) => {
    release = resolve;
  });
  serve([
    ["GET /api/auth/me", () => me],
    ["GET /api/cart", () => json(200, CART)],
  ]);
  assert.equal(currentAccount(), undefined);
  const checked = useAuth.getState().refresh();
  const waiting = api("/cart");
  await settle();
  release(UNAVAILABLE());
  await checked;

  await assert.rejects(waiting, unknownAccount);
  assert.equal(currentAccount(), undefined);
  assert.deepEqual(state(), { user: null, ready: false, failed: true });
  assert.deepEqual(requests("GET /api/cart"), [], "nothing is sent as a guest's");
});

test("the next request that needs the account asks again and goes once it is known", async () => {
  serve([
    ["GET /api/auth/me", () => json(200, A)],
    ["GET /api/cart", () => json(200, CART)],
  ]);
  assert.deepEqual(await api("/cart"), CART);
  assert.deepEqual(sent, [
    { call: "GET /api/auth/me", claimed: null },
    { call: "GET /api/cart", claimed: "101" },
  ]);
  assert.deepEqual(state(), { user: 101, ready: true, failed: false });
});

test("a check that cannot reach the server after an account change refuses a write at once and recovers", async () => {
  serve([
    ["GET /api/auth/me", UNREACHABLE],
    ["POST /api/cart/items", () => json(200, CART)],
  ]);
  accountChanged();
  await settle();
  assert.equal(currentAccount(), undefined);
  assert.deepEqual(state(), { user: null, ready: false, failed: true });

  const write = api("/cart/items", { method: "POST", body: JSON.stringify({ product_id: 17 }) });
  await assert.rejects(write, unknownAccount);
  assert.deepEqual(requests("POST /api/cart/items"), []);
  assert.equal(requests("GET /api/auth/me").length, 2, "the write asked once more before it was refused");

  routes.set("GET /api/auth/me", () => json(200, A));
  await useAuth.getState().refresh();
  assert.deepEqual(state(), { user: 101, ready: true, failed: false });
  assert.equal(currentAccount(), 101);
});

test("what failed while the account was unknown is fetched again once it is known", async () => {
  serve([
    ["GET /api/auth/me", UNAVAILABLE],
    ["GET /api/cart", () => json(200, CART)],
  ]);
  accountChanged();
  await settle();
  const observer = new QueryObserver(queryClient, { queryKey: ["cart"], queryFn: () => api("/cart"), retry: false });
  const stop = observer.subscribe(() => {});
  await settle();
  assert.equal(observer.getCurrentResult().status, "error");

  routes.set("GET /api/auth/me", () => json(200, A));
  await useAuth.getState().refresh();
  await settle();
  stop();
  assert.deepEqual(observer.getCurrentResult().data, CART);
  assert.deepEqual(
    requests("GET /api/cart").map((request) => request.claimed),
    ["101"],
  );
});

test("a failed check keeps a known account as it is", async () => {
  serve([["GET /api/auth/me", UNAVAILABLE]]);
  await useAuth.getState().refresh();
  assert.deepEqual(state(), { user: 101, ready: true, failed: false });
  assert.equal(currentAccount(), 101);
});

test("a refused check is the answer that nobody is signed in", async () => {
  serve([["GET /api/auth/me", () => json(401, { detail: "Не авторизован" })]]);
  accountChanged();
  await settle();
  assert.deepEqual(state(), { user: null, ready: true, failed: false });
  assert.equal(currentAccount(), null);
});

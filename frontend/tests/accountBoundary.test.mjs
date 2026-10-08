import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";

import { MutationObserver } from "@tanstack/react-query";

register("./resolve-js.mjs", import.meta.url);

// a fresh process: the account is not known until the first /auth/me of these tests answers
const { useAuth } = await import("../.test-build/stores/auth.js");
const { useToast } = await import("../.test-build/stores/toast.js");
const { queryClient } = await import("../.test-build/lib/queryClient.js");
const { currentAccount } = await import("../.test-build/lib/session.js");
const { sessionMutation } = await import("../.test-build/lib/sessionCache.js");
const { addItem, cartMutation } = await import("../.test-build/api/cart.js");
const { commandDeps } = await import("../.test-build/api/employee.js");
const { OutcomeUnknown, pendingIntents, submit } = await import("../.test-build/lib/commandIntents.js");

const account = (id) => ({
  id,
  phone: null,
  name: `account ${id}`,
  is_employee: true,
  is_superuser: false,
  staff_role: "restaurant_operator",
  transitions_via_commands: true,
});
const A = account(101);
const B = account(202);
const CART = { version: 1, items: [], total: 0, count: 0, adjustments: [], removed_items: [], review_order: null };
const SWITCHED = "Аккаунт сменился в другой вкладке, данные обновлены";
const REFUSED = { code: "account_changed", detail: "Аккаунт сменился" };

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

function held() {
  let release;
  const promise = new Promise((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

// every request is recorded with the account the browser was signed in as and the account it claimed
let signedIn = null;
let routes = new Map();
const sent = [];
globalThis.document = { cookie: "csrftoken=test-token" };
globalThis.fetch = async (url, init = {}) => {
  const call = `${(init.method ?? "GET").toUpperCase()} ${url}`;
  const claimed = init.headers instanceof Headers ? init.headers.get("X-Account") : null;
  sent.push({ call, as: signedIn, claimed });
  const answer = routes.get(call);
  if (!answer) throw new Error(`no answer for ${call}`);
  return answer();
};
const requests = (call) => sent.filter((request) => request.call === call);

function serve(extra) {
  routes = new Map([["GET /api/auth/csrf", () => json(200, {})], ...extra]);
  sent.length = 0;
  useToast.getState().clear();
}

test("a request bound to an account waits until the account is known and then says whose it is", async () => {
  const me = held();
  signedIn = "A";
  serve([
    ["GET /api/auth/me", () => me.promise],
    ["POST /api/cart/items", () => json(200, CART)],
  ]);
  assert.equal(currentAccount(), undefined);
  const refreshed = useAuth.getState().refresh();
  const mutation = new MutationObserver(queryClient, sessionMutation(cartMutation(queryClient, () => {}, addItem)));
  const stop = mutation.subscribe(() => {});
  mutation.mutate({ productId: 17 }).catch(() => {});
  await settle();
  assert.deepEqual(requests("POST /api/cart/items"), [], "nothing account bound leaves before the account is known");

  me.release(json(200, A));
  await refreshed;
  await settle();
  stop();
  assert.deepEqual(requests("POST /api/cart/items"), [{ call: "POST /api/cart/items", as: "A", claimed: "101" }]);
  assert.deepEqual(requests("GET /api/auth/me"), [{ call: "GET /api/auth/me", as: "A", claimed: null }]);
});

for (const [outcome, answer] of [
  ["success", () => json(200, A)],
  ["refusal", () => json(401, { detail: "unauthorized" })],
]) {
  test(`a late /auth/me ${outcome} does not take back the account signed in after it was asked`, async () => {
    const me = held();
    serve([
      ["GET /api/auth/me", () => me.promise],
      [
        "POST /api/auth/logout",
        () => {
          signedIn = null;
          return json(200, {});
        },
      ],
    ]);
    signedIn = "A";
    useAuth.getState().setUser(A);
    const late = useAuth.getState().refresh();
    await settle();
    await useAuth.getState().logout();
    signedIn = "B";
    useAuth.getState().setUser(B);

    me.release(answer());
    await late;
    await settle();
    assert.equal(useAuth.getState().user?.id, 202);
    assert.equal(currentAccount(), 202);
  });
}

test("a write refused because the browser has another account is not repeated and the tab takes that account", async () => {
  serve([
    ["POST /api/cart/items", () => json(409, REFUSED)],
    ["GET /api/auth/me", () => json(200, B)],
  ]);
  signedIn = "A";
  useAuth.getState().setUser(A);
  queryClient.setQueryData(["cart"], { ...CART, version: 9, review_order: 77 });
  const told = [];
  const mutation = new MutationObserver(queryClient, sessionMutation(cartMutation(queryClient, (m) => told.push(m), addItem)));
  const stop = mutation.subscribe(() => {});
  signedIn = "B"; // another tab signed in
  mutation.mutate({ productId: 17 }).catch(() => {});
  await settle();
  stop();

  assert.deepEqual(requests("POST /api/cart/items"), [{ call: "POST /api/cart/items", as: "B", claimed: "101" }]);
  assert.equal(useAuth.getState().user?.id, 202);
  assert.equal(queryClient.getQueryData(["cart"]), undefined);
  assert.deepEqual(told, []);
  assert.deepEqual(
    useToast.getState().toasts.map((t) => t.message),
    [SWITCHED],
  );
});

test("logout from a tab whose account the browser no longer has neither ends that account nor shows a guest", async () => {
  serve([
    ["POST /api/auth/logout", () => json(409, REFUSED)],
    ["GET /api/auth/me", () => json(200, B)],
  ]);
  signedIn = "A";
  useAuth.getState().setUser(A);
  signedIn = "B";
  await useAuth.getState().logout();
  await settle();

  assert.deepEqual(requests("POST /api/auth/logout"), [{ call: "POST /api/auth/logout", as: "B", claimed: "101" }]);
  assert.equal(useAuth.getState().user?.id, 202);
});

test("a command refused because the browser has another account stays with the one who started it", async () => {
  const route = "POST /api/employee/orders/5/transition-commands";
  serve([
    [route, () => json(409, REFUSED)],
    ["GET /api/auth/me", () => json(200, B)],
  ]);
  signedIn = "A";
  useAuth.getState().setUser(A);
  const deps = { ...commandDeps(), sleep: async () => {}, now: () => 0 };
  signedIn = "B";

  await assert.rejects(submit(deps, A.id, { orderId: 5, expected_status: "created", to_status: "confirmed" }), OutcomeUnknown);
  await settle();
  assert.deepEqual(
    requests(route).map((request) => request.claimed),
    ["101"],
  );
  assert.equal(pendingIntents(deps.store, A.id).length, 1);
  assert.equal(pendingIntents(deps.store, B.id).length, 0);
  deps.store.removeItem(`pending-commands:${A.id}`);
});

test("the endpoints that find out or change who is signed in claim no account", async () => {
  serve([["GET /api/auth/me", () => json(200, A)]]);
  await useAuth.getState().refresh();
  for (const request of sent) {
    assert.equal(request.claimed, null, request.call);
  }
});

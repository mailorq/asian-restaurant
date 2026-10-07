import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";

import { MutationObserver, QueryObserver } from "@tanstack/react-query";

register("./resolve-js.mjs", import.meta.url);

const { useAuth } = await import("../.test-build/stores/auth.js");
const { queryClient } = await import("../.test-build/lib/queryClient.js");
const { sessionCallbacks, sessionMutation } = await import("../.test-build/lib/sessionCache.js");
const { addItem, cartMutation, cartQuery, setItem } = await import("../.test-build/api/cart.js");
const { checkoutMutation } = await import("../.test-build/api/orders.js");
const { commandDeps } = await import("../.test-build/api/employee.js");
const { OutcomeUnknown, pendingIntents, submit } = await import("../.test-build/lib/commandIntents.js");

const account = (id) => ({
  id,
  phone: null,
  name: `account ${id}`,
  is_employee: false,
  is_superuser: false,
  staff_role: null,
  transitions_via_commands: false,
});
const A = account(101);
const B = account(202);

const cart = (productId, version, reviewOrder) => ({
  version,
  items: [
    { product_id: productId, name: `product ${productId}`, category: "dish", price: 100, quantity: 1, image: null, available: true },
  ],
  total: 100,
  count: 1,
  adjustments: [],
  removed_items: [],
  review_order: reviewOrder,
});
const A_CART = cart(17, 9, 77);
const B_CART = cart(21, 5, null);
const A_ORDER = { id: 501, status: "created", items: [], total: 100, address: "A", created_at: "2026-10-06T00:00:00Z" };
const FORM = { address: "ул. Пушкина, 12", payment_method: "cash", recipient_name: "A", idempotency_key: "late" };
const COMMAND = { command_id: "c1", status: "pending", result_code: "", result_detail: "", deadline_at: null, created_at: "" };
const TRANSITION = { orderId: 5, expected_status: "created", to_status: "confirmed" };
const TOKEN = "csrftoken=test-token";

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

// answers of one route in turn, the last one for every call after
const inTurn = (...answers) => () => (answers.length > 1 ? answers.shift() : answers[0])();

// the browser sends whoever is signed in when a request leaves: every request is recorded with that account
let signedIn = null;
let routes = new Map();
const sent = [];
globalThis.document = { cookie: TOKEN };
globalThis.fetch = async (url, init = {}) => {
  const call = `${(init.method ?? "GET").toUpperCase()} ${url}`;
  sent.push({ call, as: signedIn });
  const answer = routes.get(call);
  if (!answer) throw new Error(`no answer for ${call}`);
  return answer();
};
const sentAs = (call) => sent.filter((request) => request.call === call).map((request) => request.as);

const notified = [];
const notify = (message) => notified.push(message);

function serve(extra = [], csrf = () => json(200, {})) {
  routes = new Map([
    ["GET /api/auth/csrf", csrf],
    [
      "POST /api/auth/logout",
      () => {
        signedIn = null;
        return json(200, {});
      },
    ],
    ["GET /api/cart", () => json(200, B_CART)],
    ...extra,
  ]);
  sent.length = 0;
  notified.length = 0;
}

function signInA() {
  signedIn = "A";
  useAuth.getState().setUser(A);
}

async function switchToB() {
  await useAuth.getState().logout();
  signedIn = "B";
  useAuth.getState().setUser(B);
}

function run(options, variables, callbacks) {
  const mutation = new MutationObserver(queryClient, options);
  const stop = mutation.subscribe(() => {});
  mutation.mutate(variables, callbacks).catch(() => {});
  return stop;
}

// A sends a request and its answer is held; A signs out, B signs in and loads their cart; then A's
// answer arrives. returns what B's cart view (the useCartQuery query) shows from then on
async function lateAnswer({ route, answer, options, variables, callbacks }) {
  const response = held();
  serve([[route, () => response.promise.then((r) => r.clone())]]);
  signInA();
  const stopMutation = run(options, variables, callbacks);
  await settle();

  await switchToB();
  const view = new QueryObserver(queryClient, cartQuery);
  const shown = [];
  const stopView = view.subscribe((result) => {
    if (result.data) shown.push(result.data);
  });
  await settle();
  assert.deepEqual(view.getCurrentResult().data, B_CART, "B's own cart is loaded before A's answer");
  const loads = sentAs("GET /api/cart").length;

  response.release(answer());
  await settle();
  const outcome = { current: view.getCurrentResult().data, shown, reloads: sentAs("GET /api/cart").length - loads };
  stopView();
  stopMutation();
  return outcome;
}

function assertOnlyB(outcome) {
  assert.deepEqual(outcome.current, B_CART);
  for (const shown of outcome.shown) {
    assert.deepEqual(shown, B_CART, "B's cart view showed a cart that is not B's");
  }
  assert.equal(outcome.reloads, 0);
  assert.deepEqual(notified, []);
}

test("a late success of the previous account neither reaches the next one's cart nor speaks to them", async () => {
  const told = [];
  const outcome = await lateAnswer({
    route: "POST /api/cart/items",
    answer: () => json(200, A_CART),
    options: sessionMutation(cartMutation(queryClient, notify, addItem)),
    variables: { productId: 17 },
    callbacks: sessionCallbacks({ onSuccess: () => told.push("success"), onError: () => told.push("error") }),
  });
  assertOnlyB(outcome);
  assert.deepEqual(told, []);
});

test("a late conflict of the previous account does not reach the next one's cart", async () => {
  const outcome = await lateAnswer({
    route: "PUT /api/cart/items/17",
    answer: () => json(409, { code: "cart_version_conflict", cart: A_CART }),
    options: sessionMutation(cartMutation(queryClient, notify, setItem)),
    variables: { productId: 17, quantity: 3 },
  });
  assertOnlyB(outcome);
});

test("a conflict answered after the account changed is not retried on behalf of the next one", async () => {
  const outcome = await lateAnswer({
    route: "POST /api/cart/items",
    answer: () => json(409, { code: "cart_version_conflict", cart: A_CART }),
    options: sessionMutation(cartMutation(queryClient, notify, addItem)),
    variables: { productId: 17 },
  });
  assert.deepEqual(sentAs("POST /api/cart/items"), ["A"]);
  assertOnlyB(outcome);
});

test("a late checkout of the previous account does not empty the next one's cart", async () => {
  const outcome = await lateAnswer({
    route: "POST /api/orders/checkout",
    answer: () => json(200, A_ORDER),
    options: sessionMutation(checkoutMutation(queryClient)),
    variables: FORM,
  });
  assertOnlyB(outcome);
});

test("a write still waiting for its csrf token is not sent once the account changed", async () => {
  const token = held();
  serve([["POST /api/cart/items", () => json(200, B_CART)]], inTurn(() => token.promise, () => json(200, {})));
  document.cookie = "";
  try {
    signInA();
    const stop = run(sessionMutation(cartMutation(queryClient, notify, addItem)), { productId: 17 });
    await settle();
    await switchToB();
    token.release(json(200, {}));
    await settle();
    stop();
  } finally {
    document.cookie = TOKEN;
  }
  assert.deepEqual(sentAs("POST /api/cart/items"), []);
  assert.deepEqual(notified, []);
});

test("a retry after a conflict that waited for its csrf token is not sent once the account changed", async () => {
  const token = held();
  serve(
    [["POST /api/cart/items", () => json(409, { code: "cart_version_conflict", cart: A_CART })]],
    inTurn(
      () => json(200, {}),
      () => token.promise,
      () => json(200, {}),
    ),
  );
  document.cookie = "";
  try {
    signInA();
    const stop = run(sessionMutation(cartMutation(queryClient, notify, addItem)), { productId: 17 });
    await settle();
    await switchToB();
    token.release(json(200, {}));
    await settle();
    stop();
  } finally {
    document.cookie = TOKEN;
  }
  assert.deepEqual(sentAs("POST /api/cart/items"), ["A"]);
  assert.deepEqual(notified, []);
});

test("a command watched across an account change sends nothing for the next account and stays with its own", async () => {
  const interval = held();
  // the command has an outcome by now, so a read that does go out ends the watch instead of polling on
  serve([
    ["POST /api/employee/orders/5/transition-commands", () => json(200, COMMAND)],
    ["GET /api/employee/commands/c1", () => json(200, { ...COMMAND, status: "succeeded" })],
  ]);
  signInA();
  const deps = { ...commandDeps(), sleep: () => interval.promise, now: () => 0 };
  const flow = submit(deps, A.id, TRANSITION);
  await settle();
  await switchToB();
  interval.release();
  const intent = await flow;

  assert.deepEqual(sentAs("POST /api/employee/orders/5/transition-commands"), ["A"]);
  assert.deepEqual(sentAs("GET /api/employee/commands/c1"), []);
  assert.equal(intent.state, "pending");
  assert.deepEqual(
    pendingIntents(deps.store, A.id).map((kept) => kept.key),
    [intent.key],
  );
  deps.store.removeItem(`pending-commands:${A.id}`);
});

test("a command resend after an unanswered send is not made for the next account", async () => {
  const backoff = held();
  serve([["POST /api/employee/orders/5/transition-commands", () => json(503, { detail: "unavailable" })]]);
  signInA();
  const deps = { ...commandDeps(), sleep: () => backoff.promise, now: () => 0 };
  const flow = submit(deps, A.id, TRANSITION);
  await settle();
  await switchToB();
  backoff.release();

  await assert.rejects(flow, OutcomeUnknown);
  assert.deepEqual(sentAs("POST /api/employee/orders/5/transition-commands"), ["A"]);
  assert.equal(pendingIntents(deps.store, A.id).length, 1);
  deps.store.removeItem(`pending-commands:${A.id}`);
});

// controls: the same runs without the session stamp put the previous account's answer on B's screen,
// so the late-answer tests above fail on foreign data when the boundary is gone
test("without the session stamp a late cart answer of the previous account lands in the next one's cart (control)", async () => {
  const outcome = await lateAnswer({
    route: "POST /api/cart/items",
    answer: () => json(200, A_CART),
    options: cartMutation(queryClient, notify, addItem),
    variables: { productId: 17 },
  });
  assert.equal(outcome.current.items[0].product_id, 17);
  assert.equal(outcome.current.review_order, 77);
  assert.equal(outcome.reloads, 0);
});

test("without the session stamp a late checkout of the previous account empties the next one's cart (control)", async () => {
  const outcome = await lateAnswer({
    route: "POST /api/orders/checkout",
    answer: () => json(200, A_ORDER),
    options: checkoutMutation(queryClient),
    variables: FORM,
  });
  assert.ok(outcome.shown.some((shown) => shown.version === 0 && shown.items.length === 0));
});

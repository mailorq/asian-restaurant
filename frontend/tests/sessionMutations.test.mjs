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

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

// a server whose answer to one route is held until the test lets it go
let routes = new Map();
const calls = [];
globalThis.document = { cookie: "csrftoken=test-token" };
globalThis.fetch = async (url, init = {}) => {
  const call = `${(init.method ?? "GET").toUpperCase()} ${url}`;
  calls.push(call);
  const answer = routes.get(call);
  if (!answer) throw new Error(`no answer for ${call}`);
  return answer();
};
const count = (call) => calls.filter((c) => c === call).length;

const notified = [];
const notify = (message) => notified.push(message);

// A sends a request and its answer is held; A signs out, B signs in and loads their cart; then A's
// answer arrives. returns what B's cart view (the useCartQuery query) shows from then on
async function lateAnswer({ route, answer, options, variables, callbacks }) {
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  routes = new Map([
    ["GET /api/auth/csrf", () => json(200, {})],
    ["POST /api/auth/logout", () => json(200, {})],
    ["GET /api/cart", () => json(200, B_CART)],
    [route, () => held.then((response) => response.clone())],
  ]);
  calls.length = 0;
  notified.length = 0;

  useAuth.getState().setUser(A);
  const mutation = new MutationObserver(queryClient, options);
  const stopMutation = mutation.subscribe(() => {});
  mutation.mutate(variables, callbacks).catch(() => {});
  await settle();

  await useAuth.getState().logout();
  useAuth.getState().setUser(B);
  const view = new QueryObserver(queryClient, cartQuery);
  const shown = [];
  const stopView = view.subscribe((result) => {
    if (result.data) shown.push(result.data);
  });
  await settle();
  assert.deepEqual(view.getCurrentResult().data, B_CART, "B's own cart is loaded before A's answer");
  const loads = count("GET /api/cart");

  release(answer());
  await settle();
  const outcome = {
    current: view.getCurrentResult().data,
    shown,
    reloads: count("GET /api/cart") - loads,
    sent: count(route),
  };
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
  assert.equal(outcome.sent, 1);
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

// controls: the same runs without the session stamp put the previous account's answer on B's screen,
// so the tests above fail on foreign data when the boundary is gone
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

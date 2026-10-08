import assert from "node:assert/strict";
import { register } from "node:module";
import test from "node:test";

register("./resolve-js.mjs", import.meta.url);

const { useAuth } = await import("../.test-build/stores/auth.js");

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

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

let routes = new Map();
globalThis.document = { cookie: "csrftoken=test-token" };
globalThis.fetch = async (url, init = {}) => {
  const call = `${(init.method ?? "GET").toUpperCase()} ${url}`;
  const answer = routes.get(call);
  if (!answer) throw new Error(`no answer for ${call}`);
  return answer();
};

for (const [outcome, answer] of [
  ["success", () => json(200, A)],
  ["refusal", () => json(401, { detail: "unauthorized" })],
]) {
  test(`a late /auth/me ${outcome} does not take back the account signed in after it was asked`, async () => {
    let release;
    const me = new Promise((resolve) => {
      release = resolve;
    });
    routes = new Map([
      ["GET /api/auth/me", () => me],
      ["GET /api/auth/csrf", () => json(200, {})],
      ["POST /api/auth/logout", () => json(200, {})],
    ]);
    useAuth.getState().setUser(A);
    const late = useAuth.getState().refresh();
    await settle();
    await useAuth.getState().logout();
    useAuth.getState().setUser(B);

    release(answer());
    await late;
    await settle();
    assert.equal(useAuth.getState().user?.id, 202);
  });
}

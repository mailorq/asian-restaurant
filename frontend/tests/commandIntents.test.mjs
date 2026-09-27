import assert from "node:assert/strict";
import test from "node:test";

import { modeChanged, pendingIntents, resume, submit } from "../.test-build/lib/commandIntents.js";

const USER = 7;
const ORDER = { orderId: 12, expected_status: "created", to_status: "confirmed" };

class MemoryStore {
  data = new Map();
  getItem(key) {
    return this.data.has(key) ? this.data.get(key) : null;
  }
  setItem(key, value) {
    this.data.set(key, String(value));
  }
  removeItem(key) {
    this.data.delete(key);
  }
}

const failure = (status, body = {}) => Object.assign(new Error(`status ${status}`), { status, body });

// operations as the panel meets it: one command per key, answers that can be lost on the way back
function operations() {
  const byKey = new Map();
  const byId = new Map();
  const server = {
    sent: [],
    losses: 0,
    refusal: null,
    states: [],
    byKey,
    async send(_transition, key) {
      server.sent.push(key);
      if (server.refusal) throw server.refusal;
      let command = byKey.get(key);
      if (!command) {
        command = { command_id: `c${byKey.size + 1}`, status: "pending", result_code: "", result_detail: "", deadline_at: null, created_at: "" };
        byKey.set(key, command);
        byId.set(command.command_id, command);
      }
      if (server.losses > 0) {
        server.losses -= 1;
        throw server.losses === 2 ? failure(0) : failure(504, { code: "outcome_unknown" });
      }
      return { ...command };
    },
    async read(commandId) {
      const command = byId.get(commandId);
      if (server.states.length) command.status = server.states.shift();
      return { ...command };
    },
  };
  return server;
}

let keys = 0;
// a fresh set of functions over the same storage is what a reload of the tab leaves
function tab(server, store) {
  let clock = 0;
  return {
    send: server.send,
    read: server.read,
    store,
    sleep: async (ms) => {
      clock += ms;
    },
    newKey: () => `key-${++keys}`,
    now: () => clock,
  };
}

test("the server took the first attempt and three answers were lost: after a reload the same key finishes it", async () => {
  const server = operations();
  const store = new MemoryStore();
  server.losses = 3;

  await assert.rejects(submit(tab(server, store), USER, ORDER));
  const [kept] = pendingIntents(store, USER);
  assert.ok(kept, "the unresolved action survives in storage");
  server.states = ["succeeded"];
  const done = await resume(tab(server, store), USER, kept.key);

  assert.equal(done.state, "succeeded");
  assert.deepEqual([...new Set(server.sent)], [kept.key]);
  assert.equal(server.byKey.size, 1);
  assert.deepEqual(pendingIntents(store, USER), []);
});

test("the same action clicked again after an unknown outcome goes out under the same key", async () => {
  const server = operations();
  const store = new MemoryStore();
  server.losses = 3;
  await assert.rejects(submit(tab(server, store), USER, ORDER));

  server.states = ["succeeded"];
  const done = await submit(tab(server, store), USER, ORDER);

  assert.equal(done.state, "succeeded");
  assert.equal(new Set(server.sent).size, 1);
  assert.equal(server.byKey.size, 1);
});

for (const open of ["timed_out", "dispatch_failed"]) {
  test(`${open} is no outcome: the command stays watched and a later refresh learns how it ended`, async () => {
    const server = operations();
    const store = new MemoryStore();
    server.states = [open];

    const first = await submit(tab(server, store), USER, ORDER);
    assert.equal(first.state, open);
    const [kept] = pendingIntents(store, USER);
    assert.equal(kept?.commandId, first.commandId);

    server.states = ["succeeded"];
    const done = await resume(tab(server, store), USER, kept.key);
    assert.equal(done.state, "succeeded");
    assert.equal(server.sent.length, 1, "a known command is read, never sent again");
    assert.deepEqual(pendingIntents(store, USER), []);
  });
}

test("a definitive refusal forgets the action", async () => {
  const server = operations();
  const store = new MemoryStore();
  server.refusal = failure(409);

  await assert.rejects(submit(tab(server, store), USER, ORDER));

  assert.deepEqual(pendingIntents(store, USER), []);
});

test("a changed way of changing status is told apart from other refusals and forgets the action", async () => {
  const server = operations();
  const store = new MemoryStore();
  const changed = failure(403, { code: "transition_mode_changed", detail: "x" });
  server.refusal = changed;

  await assert.rejects(submit(tab(server, store), USER, ORDER));

  assert.equal(modeChanged(changed), true);
  assert.equal(modeChanged(failure(403, { detail: "Недостаточно прав" })), false);
  assert.deepEqual(pendingIntents(store, USER), []);
});

test("one employee never sees the unresolved actions of another", async () => {
  const server = operations();
  const store = new MemoryStore();
  server.losses = 3;
  await assert.rejects(submit(tab(server, store), USER, ORDER));

  assert.deepEqual(pendingIntents(store, USER + 1), []);
});

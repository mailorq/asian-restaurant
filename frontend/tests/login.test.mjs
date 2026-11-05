import assert from "node:assert/strict";
import test from "node:test";

const { isValidLogin } = await import("../.test-build/lib/login.js");

// the same cases as backend/tests/test_login_rule.py: what the form lets through, the server takes
const ACCEPTED = ["abc", "Noodle.Fan", "a_b-c.d", `a${"b".repeat(31)}`, "\u212aat"];
const REFUSED = [
  "ab",
  `a${"b".repeat(32)}`,
  "1abc",
  "_abc",
  "\u017fam",
  "\u0131van",
  "\u0430bc",
  "ab c",
  "+380671111111",
  "",
];

test("the form takes the logins the server takes", () => {
  for (const login of ACCEPTED) assert.equal(isValidLogin(login), true, JSON.stringify(login));
});

test("the form refuses the logins the server refuses", () => {
  for (const login of REFUSED) assert.equal(isValidLogin(login), false, JSON.stringify(login));
});

# ADR-0002 - where the storefront BFF keeps the state of a command retry

## Status
**Accepted**, before the BFF is built. Covers `POST /api/employee/orders/{id}/transition-commands`.

## Context
The browser changes an order status through the storefront, which calls the Operations command API
(ADR-0001) with a JWT it mints itself. The browser sends `Idempotency-Key` once per employee action and
repeats the same key when the outcome of a request is unknown.

Operations is the source of truth for idempotency. Its identity is
`(actor_id, command_type, target, idempotency_key)` under a unique constraint, the key is stripped,
at most 200 characters, with no control characters. A repeat of the identity with the same intent
returns the existing command (200), with another intent a conflict (409). Commands are never deleted,
so the identity is kept forever. Operations charges its limit on new commands only after it has looked
the identity up, so a repeat is never charged (see `operations/ratelimit.py`).

An earlier plan let the BFF recognise repeats itself, keyed by `actor_id` and the key. Operations
treats the same key on another order as a new command, so the BFF waved new commands past its limit.

## Options
1. **A reservation in Redis with a bounded lifetime.** Safe only if the BFF identity is exactly the
   Operations one; its lifetime is then a guess at how late a browser may repeat. A repeat after
   expiry is charged as new, a reservation that outlives the Operations identity would let a new
   command through uncounted. Redis and PostgreSQL are not one transaction, so "charged once" holds
   only with a protocol nobody has proven.
2. **A permanent table in the storefront database.** Mirrors the Operations identity exactly and
   lives as long, but it is a second register of commands: it grows without bound, duplicates
   Operations, and still meets Redis outside a transaction for the limit.
3. **No retry state in the BFF.** The BFF forwards the key untouched and never decides what a repeat
   is. Operations decides, and its limit on new commands sits behind its own lookup. The BFF only
   bounds the request rate per employee, repeats included, against floods.

## Decision
Option 3. The BFF keeps no state about commands or keys:

- it validates the key by the Operations rules and forwards the stripped key, so a malformed key is
  refused before a token is minted, and every accepted key is one Operations accepts;
- it limits `POST` and `GET` per employee in fixed windows, counting every request; a repeat counts,
  which is the point: the bound is on load, not on commands;
- `429` and `503` from Operations reach the browser with their `Retry-After`;
- a timeout after the request left the BFF is an unknown outcome (`504`, `outcome_unknown`): the
  browser repeats the same action with the same key, never another write path.

## Consequences
- The bypass through one key on several orders cannot happen: the BFF has no notion of a repeat.
- A late repeat is answered by Operations as long as the command exists, which is forever.
- A flood of repeats is refused by the BFF request limit with `429` and `Retry-After`.
- Tests pin the forwarding: the key and the body reach Operations unchanged, a repeat past the
  Operations limit is still `200`, and a timeout is `504` with no second write path.

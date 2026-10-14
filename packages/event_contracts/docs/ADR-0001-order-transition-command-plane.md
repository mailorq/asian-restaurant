# ADR-0001 - order transition command plane

## Status
**Accepted**. Covers `orders.transition` only; `stock.adjust` gets its own decision record.

## Context
Staff change order status through the storefront's employee API, which writes the order directly.
Operations is a separate service: own database, own credentials, projections built from versioned
events. To let an operations UI drive order state **without** giving operations write access to the
storefront database - and without creating a second direct write path - operations issues an
intent, the storefront (sole writer) applies it, and the outcome returns as a versioned event.

## Decision, flow
1. **Operations API** accepts an employee JWT. `actor_id` is taken **only** from the verified token
   subject and coerced to `int` (the subject is issued as a string), never from the body. In one
   operations-DB transaction it idempotently creates an `OperationCommand`, an `OperationAuditLog`
   row, and one `orders.transition.requested.v1` row in `OperationsOutbox`. Operations never
   touches the storefront database.
2. **Operations outbox relay** publishes the request with publisher confirms directly to the
   storefront `commands` exchange (see Transport), then moves the command to `dispatched`.
3. **Storefront command consumer** is the ONLY writer. In one transaction it: dedups by
   `command_id`; rejects an expired command; **re-authorizes the actor under lock**; loads and
   locks the order; checks `expected_status` and transition validity; applies the change through
   `orders.service.transition`; records `CommandInbox` with the serialized outcome; and writes the
   outcome event to `OrderOutbox`. A redelivered command replays the stored outcome verbatim.
4. **Bridge** relays the outcome to `operations.events` under the permissions it already holds.
5. **Operations outcome consumer** finalizes the command through the single transactional
   dispatcher below.

## Transport
Operations publishes commands **directly** to the storefront vhost with a dedicated,
least-privilege credential. No extra relay process and no second retry/DLQ surface, at the cost
of one narrow write grant.

- new RabbitMQ user **`operations_commands`**, used **only** by the operations outbox relay;
- storefront vhost permissions: `configure=^$`, `write=^commands$`, `read=^$`;
- its own setting/env var **`OPERATIONS_COMMANDS_RABBITMQ_URL`** - never `OPERATIONS_RABBITMQ_URL`
  and never the consumer's credential;
- because `configure` is empty, the publisher **cannot and must not declare** any exchange, queue
  or binding. The storefront command consumer owns and declares the whole command topology
  (`commands` exchange, `commands.orders` queue, retry queue, `commands.orders.dlq`);
- `definitions.dev.json` and `provision.sh` must be changed **together**, and `provision.sh` must
  also `revoke` the new user everywhere it must not reach (`/` and `operations`): isolation is
  enforced, not merely granted.

Outcomes travel the **existing** storefront outbox to the `orders` exchange, then the bridge to
`operations.events`. Routing keys are `order.transition_succeeded` / `order.transition_rejected`,
so the outcome direction needs no new exchange or permission. There is no
`operations.commands.outcomes` exchange.

They do get their own queue on each hop - `operations.bridge.outcomes` on the storefront vhost and
`operations.outcomes` on the operations vhost - because a command has a deadline and a shared queue
puts it behind whatever projection traffic arrived first, a `snapshot.*` backfill included. A
transition the storefront applied would then be read as `timed_out`. So `order.*` is enumerated as
`order.created` / `order.status_changed` rather than matched, and both declare passes drop the
catch-all bindings afterwards: an already running broker converges instead of double-delivering.
`CommandOutcomeBacklog` fires when anything waits in `operations.outcomes` for a minute, which is
already past the deadline it would miss.

## Contract
Events - `schema_version` 1, shared `Envelope`, aggregate `(order, order_id)`:

| event | data |
|-------|------|
| `orders.transition.requested.v1` | `command_id`, `actor_id`(>0), `actor_authz_version`(>=1), `expires_at`, `order_id`(>0), `expected_status`, `target_status` (differs from expected), `reason` (<=255) |
| `orders.transition.succeeded.v1` | `command_id`, `order_id`, `from_status`, `status` (differs from from_status) |
| `orders.transition.rejected.v1` | `command_id`, `order_id`, `reject_code`, optional `current_status`, `detail` (<=255) |

`producer` MUST be `operations` for the request and `storefront` for both outcomes.
`aggregate.version` is traceability only; the concurrency guard is `expected_status` checked under
lock. Outcome envelopes MUST carry `correlation_id` equal to the command's and `causation_id`
equal to the request event's `event_id`.

**Reject codes** and the `current_status` rule. The consumer evaluates in a fixed order, which
makes the rule deterministic: `current_status` is present **iff** the order was loaded and locked,
i.e. only for `stale_status` and `invalid_transition`.

| order | code | order loaded? |
|-------|------|---------------|
| 1 | `command_expired` - `expires_at` passed | no |
| 2 | `actor_not_authorized` - actor inactive, role gone, or `authz_version` moved | no |
| 3 | `order_not_found` | no |
| 4 | `stale_status` - not at `expected_status` | yes |
| 5 | `invalid_transition` - target unreachable | yes |

## Actor re-authorization (mandatory)
The JWT is verified when the command is **created**; the transition happens later, so the token's
authority may already be revoked. Before touching the order the storefront locks the actor row and
requires `user.is_active and has_operations_role(user) and
user.authz_version == data.actor_authz_version`. Otherwise the command is rejected with
`actor_not_authorized` and the order is not modified. This also invalidates commands minted before
a revocation.

## Command states, expiry and delivery uncertainty
```
pending         -> dispatched         request confirmed to the broker
pending         -> dispatch_failed    publish retries exhausted (delivery UNKNOWN)
dispatch_failed -> dispatched         successful re-publish
pending         -> rejected           expired BEFORE dispatch (never published; decided locally)
{pending, dispatched} -> timed_out    deadline passed, delivery attempted or unknown
{pending, dispatched, timed_out, dispatch_failed} -> succeeded | rejected   outcome applied
{timed_out, dispatch_failed} -> succeeded | rejected   operator verdict (manual, audited)
pending         -> rejected            the broker returned every attempt (delivery DISPROVED)
pending         -> rejected            the row can never form a valid envelope (quarantine)
```
**Terminal = `{succeeded, rejected}`.** `command_expired` and `actor_not_authorized` are reject
codes inside `rejected`, not extra states.

The command outbox row carries its own end states, and only `published` means a publisher confirm:
```
published    the broker confirmed the request
suppressed   expired before any publish attempt, decided locally
settled      an outcome proved delivery while the confirm was lost
failed       publish attempts spent; delivery unknown, never republished
```
`settled` exists because an outcome is stronger evidence than a confirm: it can only have been
produced by a storefront that received the request. Without it a row whose confirm never arrived
would stay `pending` forever behind a terminal command - no claim ever takes a row whose command
is finished, so it would hold the head of the backlog and keep the oldest-pending alert firing.
It is not folded into `published`, which has to keep meaning "the broker confirmed this".

Expiry is **executable**, not just a UI label:
- the relay finalizes an expired command locally as `rejected/command_expired` **only while it is
  still `pending` and provably never reached the network**. `OperationsOutbox.publish_attempted_at`
  is stamped inside the same transaction that leases the row for publishing, so a lost confirm
  still counts as a possible delivery and `pending` alone is not proof. The suppressed row moves
  to a terminal outbox status so it is never published afterwards. `timed_out` is never downgraded
  to `rejected` locally, otherwise the terminal state would depend on which worker ran first;
- claiming and expiry both lock `OperationsOutbox` before `OperationCommand`, so the relay and the
  sweeper cannot deadlock against each other;
- a row is claimable only while no unexpired lease holds it. The claim mints a `lease_token`, and
  publish completion and retry scheduling are conditional on it, so a worker superseded after a
  lease timeout can no longer touch the row. `locked_by` alone is not enough because a process
  identity can repeat after a restart;
- one operation decides and reports the reason, `claimed`, `busy`, `expired` or `unavailable`, so
  an expired command that the claim refuses is finalized on the spot rather than left pending;
- an expired command that was already attempted keeps being published: the storefront rejects it
  with `command_expired` and that outcome finalizes it, which resolves the unknown state instead
  of leaving it open;
- a publisher confirm is one fact about two records and is written in one transaction: the row
  becomes `published` and the command advances `{pending, dispatch_failed} -> dispatched`
  together. Written separately, a relay that dies between them leaves a published row beside a
  pending command, which nothing can advance and the sweeper would later call `timed_out` despite
  a confirmed delivery. The row is published unconditionally because the confirm is not in doubt;
  only the command transition is conditional, so a command already past its deadline stays
  `timed_out` and waits for an outcome;
- the claim decides on a clock read taken **after** both locks are held. The command lock can
  block for as long as another writer holds it, and a deadline that lapses during that wait must
  not be judged by the earlier read;
- once **dispatched**, expiry may only produce the non-terminal `timed_out`: absence of an outcome
  is not proof of non-delivery. A late message is rejected by the consumer with `command_expired`,
  and that outcome finalizes the command;
- a command already applied (present in `CommandInbox`) replays its **stored** outcome even after
  `expires_at`, so expiry never rewrites history.

## Idempotency and stable identifiers
Creation is idempotent on a client-supplied `Idempotency-Key`, enforced by
`unique(actor_id, command_type, target, idempotency_key)`. Same identity plus same payload returns
the existing command; same key with a different payload is a conflict; different actors never
collide.

Event IDs are **stable and stored**, never minted at publish time:
- `OperationCommand.request_event_id` - the request envelope's `event_id`, reused on every retry;
- `CommandInbox.outcome_event_id` - reused on every replay, so a duplicate command never produces a
  second, different outcome event.

## Outbox metadata is first-class on both sides
Envelopes are assembled from typed columns, never re-derived from an unvalidated JSON payload.
- `OperationsOutbox`: `command` (nullable OneToOne), `producer`, `aggregate_id`,
  `aggregate_version`, `causation_id` (alongside the existing `event_id`, `correlation_id`),
  plus the attempt columns `publish_attempted_at` and `lease_token`.
- `OrderOutbox`: `causation_id`.
- `CommandInbox`: `command_id` (unique), `request_event_id`, `correlation_id`, `outcome_event_id`,
  `outcome_type`, `outcome_data`.

## One transactional dispatcher, one inbox
`projection.apply()` writes `InboxEvent` first and commits it with the projection. Calling it and
then applying the outcome in a second step would leave the inbox row committed when the outcome
step fails: the redelivery becomes a no-op and the command never completes. There must be exactly
one inbox mechanism per event id:

The lock also re-checks provenance: an outcome is applied only when its `causation_id` equals the
command's own `request_event_id`. A misrouted or forged outcome fails this check and the inbox
write rolls back with it, so the redelivery is handled again rather than silently accepted.

```
parse -> single transaction.atomic():
           inbox dedup (once)
           outcome event -> apply_transition_outcome() under lock, causation_id checked
           other event   -> projection apply without a second inbox write
         commit -> ack
```

## Retry, failure and DLQ semantics
- **broker/network unavailable** - nothing was confirmed: the outbox row stays `pending` with
  exponential backoff and raises an age alert. A DLQ is unreachable in this state by definition, so
  it is never claimed. Once the publish attempts are exhausted the command is additionally marked
  `dispatch_failed` (non-terminal) so it is visible to reconciliation, and the row becomes `failed`:
  the attempts are spent, so nothing republishes it and only an outcome or an operator can close it.
- **exchange or binding missing (404 / unroutable) with a live broker** - the storefront consumer
  owns that topology and the publisher holds `configure=^$`, so this is an operator problem and
  nothing was delivered. The retry is rare and fixed rather than exponential, because the repair
  is an operator action; once its budget is spent the command is closed as `rejected/undeliverable`.
  It is never `dispatch_failed`, which would claim delivery is in doubt when it demonstrably never
  happened. A channel the broker *closes* is different: the close can follow acceptance, so that
  path keeps the bounded retry into `dispatch_failed`.

  A row whose envelope violates the contract is quarantined at once as `rejected/invalid_payload`:
  no later attempt can make it valid, and retrying it forever only hides the backlog behind it.

  The relay's whole retry schedule has to fit inside `COMMAND_TTL`, or a command can only ever
  arrive already expired. That is asserted from the constants themselves, not from a comment.
- **poison message on the consumer** - dead-lettered to `commands.orders.dlq` by the broker.
- `timed_out` and `dispatch_failed` are operational states requiring alert and reconciliation; the
  UI must render them as "delivery unconfirmed", never as "nothing happened".
- neither state has an automatic exit, because the service has no evidence about delivery either
  way. An operator who checked the order in the storefront supplies that evidence with
  `manage.py resolve_command <command_id> --outcome succeeded|rejected --operator <who> --reason
  <what was checked>`, which records the author and the grounds in the audit log and closes the
  outbox row. It is a shell command, not an API endpoint: the verdict asserts storefront state
  that no employee request can prove. A late outcome that contradicts the verdict wins and is
  audited as `manual_resolution_overruled` - the storefront is the sole writer of order state.

## API
```
POST /ops-api/orders/{order_id}/transition-commands     Idempotency-Key: <uuid>
     body: expected_status, target_status, reason
     202 Accepted + Location (created) | 200 OK (identical retry) | 409 (key reused with a
     different payload) | 422 (invalid input) | 401 (missing/invalid token)
GET  /ops-api/commands/{command_id}                      author-scoped
```
The idempotency key travels as a **header**, not a body field.

The actor is `int(sub)` of the verified token, typed once at the authentication boundary; no
handler re-parses the token and a body field named `actor_id` is ignored. Creation does not check
the local `OperationOrder` projection: it is a read model that can lag, and the storefront is the
authority on whether the order exists, so an unknown order comes back as an `order_not_found`
outcome rather than a 404 at request time. The read endpoint is scoped to the author, so another
employee's command answers 404 rather than confirming it exists.

## Direct paths
`POST /api/employee/orders/{id}/transition` stays for employees who are not on the command plane
(`transitions_via_commands`), and the operations UI never calls it. `POST /api/employee/inventory/{id}/adjust`
has no command-plane counterpart, because there is no `stock.adjust` command, so it is the supported path
for stock.

## Observability
Metrics: command-outbox oldest-pending age, command state counters, `timed_out` /
`dispatch_failed` gauges, DLQ depth, dispatch and outcome latency. The existing alert
`rabbitmq_queue_messages{queue=~"operations.*dlq"}` does **not** match `commands.orders.dlq`; the
command DLQ needs its own rule or a widened matcher, otherwise poison commands are silent.

## Test matrix
Unit (both sides), contract, integration over a real broker, and failure/retry. Beyond the happy
path, this iteration must cover: actor revoked between creation and consumption; command expired before
and after publish; consumer applied the change but died before ack (replay must reproduce the same
`outcome_event_id`); outcome handler failure must not leave `InboxEvent` committed;
`operations_commands` cannot read, declare or publish anywhere else; `UnroutableError` leaves the
command recoverable; duplicate outcome does not re-write status or audit; two relay/sweeper
instances do not conflict; the command DLQ is covered by an alert. Every new regression test must
be shown to fail against the pre-fix code.

## Preconditions for the RabbitMQ change
The broker change may only land together with: permission tests,
`definitions.dev.json` / `provision.sh` parity (including revokes), the new mandatory
`OPERATIONS_COMMANDS_RABBITMQ_URL`, and an updated production config guard - including raised
`DJANGO_PRODUCTION` / `OPERATIONS_PRODUCTION` service counters, which are hard-coded thresholds
that would otherwise let a new service ship without its production guard.

## Sweeper
Timeout/expiry sweeping is a service function called from the relay loop and also exposed as a
standalone management command for manual recovery.

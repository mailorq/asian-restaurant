# Order Operations service

Independent service for restaurant staff. Owns only its own read models and
command state; it never imports storefront ORM and never touches the storefront
database. Fed by versioned domain events over RabbitMQ.

## Data ownership

- Storefront is the single writer of `Order`, `Product/stock`, customer identity
  and checkout invariants.
- Operations builds projections from events and (Phase 3) requests changes via
  async commands; it is never a second writer of order status.
- Separate databases, separate roles: `ops_user` cannot authenticate to the
  storefront DB and vice versa. No shared Django session, `SECRET_KEY` or creds.

## Contracts

Versioned Pydantic contracts live in `packages/event_contracts` (no Django). A
single envelope (`event_id`, `event_type`, `schema_version`, `occurred_at`,
`producer`, `aggregate{type,id,version}`, `correlation_id`, `causation_id`,
`trace_id`, `data`) wraps every event and command. Backward-compatible additions
are ignored by older consumers; breaking changes require a new event version.

## Processes

| Process | Command |
|---|---|
| API (reads projections, issues transition commands) | `python manage.py runserver` → `/ops-api/` |
| Projection consumer | `python manage.py run_operations_consumer` |
| Command relay | `python manage.py publish_commands --loop` |
| Command sweeper (manual recovery) | `python manage.py sweep_commands` |
| Legacy bridge (temporary) | `python manage.py bridge_storefront_events` |
| Test event (verification) | `python manage.py publish_test_event --order-id N --customer-id M` |

## Messaging isolation

Storefront and operations live in **separate RabbitMQ vhosts** with separate users and
minimal permissions. Provisioning and the full permission matrix are in
[`ops/rabbitmq/README.md`](../../ops/rabbitmq/README.md) (dev template + `provision.sh`
for prod). Operations never receives the storefront AMQP URL.

The bridge is reliable: it validates each mapped envelope against the contract before
publishing, acks the legacy delivery only after the versioned publish is confirmed,
sends poison messages to its own DLQ, and delays transient failures through a bounded
retry queue (`operations.bridge.*`) before giving up to the DLQ. It preserves the
origin's `occurred_at` and keeps `producer=storefront`, tagging itself in `relayed_by`.
Transition outcomes ride their own queue on both hops (`operations.bridge.outcomes`,
`operations.outcomes`): commands carry a deadline, so they must not queue behind a projection
backfill.

## Projection version fencing

The projection advances only on `incoming_version > current_version`. A lower version is
a safe no-op (`operations_projection_events_total{outcome="stale"}`); the same version
with a different `event_id` is a `ProjectionConflict` — the consumer routes it to the DLQ
and records `outcome="conflict"` rather than overwriting. Re-delivery of the same
`event_id` is idempotent via the inbox.

## Run (dev)

```bash
# from the repo root
docker compose up -d --build operations-db operations-api operations-consumer

# migrations are their own one-shot service; inspect the schema it applied
docker compose logs operations-migrate
docker compose exec operations-db psql -U ops_user -d operations -c "\dt"

# tests
docker compose exec operations-api sh -lc "cd /srv/packages/event_contracts && python -m pytest"
docker compose exec operations-api pytest

# isolation test that needs the storefront DSN (CI/test profile only; never in prod/dev)
docker compose -f compose.yaml -f compose.test.yaml up -d db operations-api
docker compose -f compose.yaml -f compose.test.yaml exec operations-api \
  pytest tests/test_isolation.py

# shadow round-trip
docker compose exec operations-api python manage.py publish_test_event --order-id 555999 --customer-id 88
docker compose exec operations-db psql -U ops_user -d operations -c \
  "SELECT source_order_id, status FROM operations_operationorder WHERE source_order_id=555999;"
```

## Environment

| Variable | Purpose |
|---|---|
| `OPERATIONS_DATABASE_URL` | operations DB (ops role); never the storefront DB |
| `OPERATIONS_SECRET_KEY` | own secret, not shared with storefront |
| `OPERATIONS_RABBITMQ_URL` | consumer/api bus — operations vhost only |
| `OPERATIONS_BRIDGE_CONSUME_URL` | bridge input — storefront vhost (adapter user) |
| `OPERATIONS_BRIDGE_PUBLISH_URL` | bridge output — operations vhost (adapter user) |
| `IDENTITY_JWKS_URL` | Identity public keys for JWT verification |

## Auth

Identity/storefront remains the sole owner of employee role and the
`restaurant_operator` or `restaurant_manager` role. Operations does not read
`auth_user`/`groups`. Staff
exchange their session for a short-lived, RS256-signed JWT (`sub`, `roles`,
`authz_version`, `jti`, `exp`, `iss=identity`, `aud=operations`) at
`POST /api/auth/employee-token`; `EmployeeJWTAuth` verifies it against Identity's
JWKS (`GET /api/auth/jwks`) and requires a staff role that the local authorization
projection also grants. The actor
is taken from the verified token, never from a browser-supplied id: an `actor_id` in a request
body is ignored. `/ops-api/*` is staff-only; only `/health` is open, and the OpenAPI schema is served
outside production only. A transition is requested
with `POST /ops-api/orders/{order_id}/transition-commands` carrying an `Idempotency-Key` header,
and its outcome is read back from `GET /ops-api/commands/{command_id}`, scoped to its author. Revocation is enforced ahead of TTL: every request
also requires the local `EmployeeAuthorization` projection to match the token's
`authz_version` with an active role and user, failing closed on an unknown, stale, or
unavailable projection (see `ops/identity/README.md` for the propagation SLO).

## Status

Phase 1 (foundation, shadow mode). Not wired to the frontend; legacy
`/api/employee/*` remains the live path until later phases pass their checklists.

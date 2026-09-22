# RabbitMQ provisioning

Storefront and operations are isolated by **vhost + user**, with minimal permissions:

| vhost | user | configure / write / read |
|---|---|---|
| `storefront` | `storefront_app` | `.*` / `.*` / `.*` |
| `operations` | `operations_consumer` | `^operations\.` (own namespace only) |
| `storefront` | `operations_bridge` | `^operations\.bridge\.` / `^operations\.bridge\.` / `^(orders\|operations\.bridge\.)` |
| `operations` | `operations_bridge` | `^operations\.events$` / `^operations\.events$` / `^$` |
| `storefront` | `operations_commands` | `^$` / `^commands$` / `^$`, topic `^orders\.transition\.requested$` |

`operations_commands` only publishes transition commands into the storefront vhost: it can
neither declare nor repair the topology it publishes to, cannot read any queue, and its
topic permission pins it to the single routing key.

The bridge is the only cross-vhost actor; it reads `orders` on the storefront vhost
and writes only `operations.events` on the operations vhost.

## Dev

`definitions.dev.json` is a **development-only** template with placeholder passwords.
Compose mounts it and `rabbitmq.conf` loads it at boot (`load_definitions`). Do not
put real credentials in it and do not use it outside dev.

## Staging / production

Do **not** ship `definitions.dev.json`. `compose.prod.yaml` overrides the broker so the
dev template and `load_definitions` are not mounted (`volumes: !override`), boots a
baseline admin from `RABBITMQ_ADMIN_USER/PASSWORD`, and you converge the rest from
secrets with `provision.sh` — idempotent, reads passwords from the environment, safe on
an existing volume (never deletes vhosts, users, queues or messages), and it also
**revokes** any stale grants in vhosts a user must not touch:

```bash
docker compose -f compose.yaml -f compose.prod.yaml up -d rabbitmq
docker compose -f compose.yaml -f compose.prod.yaml exec -T \
  -e RABBITMQ_ADMIN_USER -e RABBITMQ_ADMIN_PASSWORD \
  -e STOREFRONT_MQ_PASSWORD -e OPERATIONS_MQ_PASSWORD -e BRIDGE_MQ_PASSWORD \
  -e OPERATIONS_COMMANDS_MQ_PASSWORD \
  rabbitmq bash -s < ops/rabbitmq/provision.sh
```

## Upgrading the broker version

4.x validates that a topic permission names an existing exchange, so `commands` is created by
`provision.sh` before the publisher is scoped to it, and by `definitions.dev.json` in dev. The
storefront consumer still owns the queues, bindings and dead-letter topology.

Upgrading a live node from 3.13 goes `3.13 -> 4.2 -> 4.3`, and every stable feature flag must be
enabled before each step:

```bash
docker compose exec -T -u rabbitmq rabbitmq rabbitmqctl list_feature_flags
```

A node with a flag left disabled refuses to start on the newer release. Khepri must stay off on
3.13: a 3.13 node with Khepri enabled cannot be upgraded in place at all, only replaced.

## Updating an existing broker without data loss

`provision.sh` converges users and permissions in place. To rotate a password on a
running broker with an existing volume:

1. set the new value in your secret store and re-run `provision.sh` (or
   `rabbitmqctl change_password <user> <new>`);
2. update the matching AMQP URL and restart the dependent services so they reconnect
   (e.g. `docker compose up -d --force-recreate operations-consumer operations-bridge`).

Never `docker compose down -v` or delete the `rabbitmqdata` volume to apply credential
changes — that destroys durable queues and unacked messages. Adding a new vhost/user is
likewise just another `provision.sh` run.

## Dead-letter queues

Both DLQ alerts have a matching operator action; neither tool consumes anything while listing.

| queue | command |
|---|---|
| `commands.orders.dlq`, and `orders.ops.dlq` where the legacy consumer runs (dev only) | `backend: manage.py dlq <queue> --list \| --replay N \| --drop N --yes --reason ...` |
| `operations.projection.dlq` | `operations: manage.py dlq --list \| --replay N \| --drop N --yes --reason ...` |

Listing prints identity only - event type, reason, message id, payload sha256 and size - because
a dead-lettered order event carries a customer phone and address, and an operator tool must not
ship those into the log collector. `--show-payload` prints the bodies when that is what is needed.
A discarded message is logged by hash and size, never by content.

Replay puts a message back only after the publish is confirmed, and clears the spent retry budget
so a fixed cause gets a fresh attempt; `x-replayed-by` and `x-replayed-at` stay on the message.
Discarding is permanent, so it needs `--yes` and `--reason`, and every discarded body is logged.

The two differ in where they republish, because the topologies differ:
- operations goes through `operations.events` with the original routing key. Only the projection
  and outcome queues are bound to it, so each message returns to the one it fell out of.
- the storefront goes straight to the owning queue. `orders` also feeds the operations bridge, so
  a republish there would project the same event a second time. `operations_consumer` could not
  do this anyway: publishing to the default exchange needs write on `amq.default`, which
  `^operations\.` does not grant.

#!/usr/bin/env bash
# Idempotent RabbitMQ provisioning for storefront/operations isolation.
# Passwords come from the environment (a secret store), never from a committed file.
# Safe to re-run and to run against an existing volume: it never deletes vhosts,
# users, queues or messages — it only converges users/permissions to the target.
#
# Required env:
#   RABBITMQ_ADMIN_USER RABBITMQ_ADMIN_PASSWORD
#   STOREFRONT_MQ_PASSWORD OPERATIONS_MQ_PASSWORD BRIDGE_MQ_PASSWORD OPERATIONS_COMMANDS_MQ_PASSWORD
# Optional env:
#   RABBITMQ_NODE  target a named node (e.g. rabbit@rabbitmq), set by the one-shot
#                  rabbitmq-provision service, which runs in the broker's network namespace
#                  because the broker's management api and erlang distribution listen on its
#                  loopback only; unset when run inside the broker.
#   RABBITMQ_MANAGEMENT_HOST RABBITMQ_MANAGEMENT_PORT  management api that declares the
#                  command exchange, localhost:15672 by default, i.e. inside the broker
#
# Run inside the broker container, e.g.:
#   docker compose exec -T \
#     -e RABBITMQ_ADMIN_USER -e RABBITMQ_ADMIN_PASSWORD \
#     -e STOREFRONT_MQ_PASSWORD -e OPERATIONS_MQ_PASSWORD -e BRIDGE_MQ_PASSWORD -e OPERATIONS_COMMANDS_MQ_PASSWORD \
#     rabbitmq bash -s < ops/rabbitmq/provision.sh
set -euo pipefail

: "${RABBITMQ_ADMIN_USER:?set RABBITMQ_ADMIN_USER}"
: "${RABBITMQ_ADMIN_PASSWORD:?set RABBITMQ_ADMIN_PASSWORD}"
: "${STOREFRONT_MQ_PASSWORD:?set STOREFRONT_MQ_PASSWORD}"
: "${OPERATIONS_MQ_PASSWORD:?set OPERATIONS_MQ_PASSWORD}"
: "${BRIDGE_MQ_PASSWORD:?set BRIDGE_MQ_PASSWORD}"
: "${OPERATIONS_COMMANDS_MQ_PASSWORD:?set OPERATIONS_COMMANDS_MQ_PASSWORD}"

ctl() {
  if [ -n "${RABBITMQ_NODE:-}" ]; then rabbitmqctl -n "$RABBITMQ_NODE" "$@"; else rabbitmqctl "$@"; fi
}

# adding a vhost that exists, clearing permissions that were never granted and closing connections that are
# not open all succeed on the pinned broker, so any failure of these is real and stops the provisioning
ensure_vhost() { ctl add_vhost "$1"; }
ensure_user() { ctl add_user "$1" "$2" 2>/dev/null || ctl change_password "$1" "$2"; }
revoke() { ctl clear_permissions -p "$1" "$2"; }

ensure_vhost /
ensure_vhost storefront
ensure_vhost operations

ensure_user "$RABBITMQ_ADMIN_USER" "$RABBITMQ_ADMIN_PASSWORD"
ctl set_user_tags "$RABBITMQ_ADMIN_USER" administrator
ensure_user storefront_app "$STOREFRONT_MQ_PASSWORD"
ensure_user operations_consumer "$OPERATIONS_MQ_PASSWORD"
ensure_user operations_bridge "$BRIDGE_MQ_PASSWORD"
ensure_user operations_commands "$OPERATIONS_COMMANDS_MQ_PASSWORD"

for v in / storefront operations; do
  ctl set_permissions -p "$v" "$RABBITMQ_ADMIN_USER" '.*' '.*' '.*'
done

ctl set_permissions -p storefront storefront_app '.*' '.*' '.*'
ctl set_permissions -p operations operations_consumer '^operations\.' '^operations\.' '^operations\.'
ctl set_permissions -p storefront operations_bridge \
  '^operations\.bridge\.' '^operations\.bridge\.' '^(orders|operations\.bridge\.)'
ctl set_permissions -p operations operations_bridge \
  '^operations\.events$' '^operations\.events$' '^$'

# command publisher: cannot declare anything and cannot read. Resource write alone would
# allow any routing key, so the routing key is pinned with a topic permission as well
ctl set_permissions -p storefront operations_commands '^$' '^commands$' '^$'
# the broker refuses a topic permission for an exchange that does not exist, so the exchange is
# provisioned here; the publisher still holds configure='^$' and cannot create or repair it
rabbitmqadmin \
  --host "${RABBITMQ_MANAGEMENT_HOST:-localhost}" \
  --port "${RABBITMQ_MANAGEMENT_PORT:-15672}" \
  --username "$RABBITMQ_ADMIN_USER" \
  --password "$RABBITMQ_ADMIN_PASSWORD" \
  --vhost storefront \
  declare exchange --name commands --type topic --durable true
# converge, do not merge: a topic permission left over from an older layout would still grant
# its routing keys
ctl clear_topic_permissions -p storefront operations_commands
ctl set_topic_permissions -p storefront operations_commands commands '^orders\.transition\.requested$' '^$'

# revoke any stale rights in vhosts each user must not touch (isolation is enforced,
# not just granted - a leftover grant from an earlier layout would breach it)
revoke / storefront_app
revoke operations storefront_app
revoke / operations_consumer
revoke storefront operations_consumer
revoke / operations_bridge
revoke / operations_commands
revoke operations operations_commands

# RabbitMQ caches authorisation per connection, so a narrowed grant does not apply to an
# already open channel until it reconnects
for u in storefront_app operations_consumer operations_bridge operations_commands; do
  ctl close_all_user_connections "$u" 'permissions reprovisioned'
done

echo "rabbitmq provisioning complete"

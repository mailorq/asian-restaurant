#!/usr/bin/env bash
# production broker bootstrap in an isolated compose project: a bare broker, the one-shot
# rabbitmq-provision service run exactly as a release runs it, then the grants it must leave
# behind. reads only tracked config and a throwaway env-file outside the checkout, and tears
# down only its own project
set -euo pipefail

PROJ="${SMOKE_PROJECT:-ar_prodsmoke_$(date +%s)_$$}"
# the image media-init pins; this test only needs a python that can reach the project network
PY_IMAGE=python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
WORK="$(mktemp -d)"
REPO="$PWD"
# docker on windows takes host paths in its own form, and git bash must not rewrite the container side
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
  REPO="$(cygpath -m "$REPO")"
fi
ENV_FILE="$WORK/production.env"

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml "$@"; }
cleanup() { echo "== teardown (only $PROJ) =="; dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

export RABBITMQ_ADMIN_USER=smoke_admin RABBITMQ_ADMIN_PASSWORD="$(rnd)"
export STOREFRONT_MQ_PASSWORD="$(rnd)" OPERATIONS_MQ_PASSWORD="$(rnd)" BRIDGE_MQ_PASSWORD="$(rnd)"
export OPERATIONS_COMMANDS_MQ_PASSWORD="$(rnd)"
printf 'throwaway\n' > "$WORK/identity.pem"
# the whole production render has to interpolate, although only the broker is started
cat > "$ENV_FILE" <<EOF
RABBITMQ_ADMIN_USER=$RABBITMQ_ADMIN_USER
RABBITMQ_ADMIN_PASSWORD=$RABBITMQ_ADMIN_PASSWORD
RABBITMQ_ERLANG_COOKIE=$(rnd)
STOREFRONT_MQ_PASSWORD=$STOREFRONT_MQ_PASSWORD
OPERATIONS_MQ_PASSWORD=$OPERATIONS_MQ_PASSWORD
BRIDGE_MQ_PASSWORD=$BRIDGE_MQ_PASSWORD
OPERATIONS_COMMANDS_MQ_PASSWORD=$OPERATIONS_COMMANDS_MQ_PASSWORD
DJANGO_SECRET_KEY=smoke-$(rnd)
DJANGO_ALLOWED_HOSTS=smoke.invalid
REDIS_URL=redis://redis:6379/1
CART_REDIS_URL=redis://redis:6379/0
RABBITMQ_URL=amqp://storefront_app:$STOREFRONT_MQ_PASSWORD@rabbitmq:5672/storefront
IDENTITY_JWT_KID=smoke-1
IDENTITY_JWT_KEY_FILE=$WORK/identity.pem
POSTGRES_DB=storefront
POSTGRES_USER=smoke
POSTGRES_PASSWORD=smoke-$(rnd)
STOREFRONT_DB_MIGRATOR_PASSWORD=$(rnd)
STOREFRONT_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=smoke
OPERATIONS_DB_PASSWORD=smoke-$(rnd)
OPERATIONS_DB_MIGRATOR_PASSWORD=$(rnd)
OPERATIONS_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_SECRET_KEY=smoke-$(rnd)
OPERATIONS_ALLOWED_HOSTS=smoke.invalid
OPERATIONS_RABBITMQ_URL=amqp://operations_consumer:$OPERATIONS_MQ_PASSWORD@rabbitmq:5672/operations
OPERATIONS_BRIDGE_CONSUME_URL=amqp://operations_bridge:$BRIDGE_MQ_PASSWORD@rabbitmq:5672/storefront
OPERATIONS_BRIDGE_PUBLISH_URL=amqp://operations_bridge:$BRIDGE_MQ_PASSWORD@rabbitmq:5672/operations
OPERATIONS_COMMANDS_RABBITMQ_URL=amqp://operations_commands:$OPERATIONS_COMMANDS_MQ_PASSWORD@rabbitmq:5672/storefront
EOF

echo "== bare broker, no dev definitions =="
dc up -d rabbitmq >/dev/null
# -u rabbitmq: a cli run as root before the server wrote .erlang.cookie creates it root-owned and the server dies reading it
ready=0
for _ in $(seq 1 60); do
  if dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q check_running >/dev/null 2>&1; then ready=1; break; fi
  sleep 3
done
[ "$ready" = 1 ] || fail "the broker never finished starting"

echo "== provisioning, as the release runs it =="
dc --profile provision run --rm rabbitmq-provision || fail "rabbitmq-provision exited non-zero"
echo "== provisioning again: converges instead of failing on existing state =="
dc --profile provision run --rm rabbitmq-provision >/dev/null || fail "a second rabbitmq-provision run failed"

echo "== grants left behind =="
q() { dc exec -T -u rabbitmq rabbitmq rabbitmqctl -q --no-table-headers "$@"; }
exchange="$(q list_exchanges -p storefront name type durable | grep "^commands	" || true)"
[ "$exchange" = "commands	topic	true" ] || fail "exchange commands: '$exchange'"
perms="$(q list_user_permissions operations_commands)"
[ "$perms" = 'storefront	^$	^commands$	^$' ] || fail "operations_commands permissions: '$perms'"
topic="$(q list_user_topic_permissions operations_commands)"
[ "$topic" = 'storefront	commands	^orders\.transition\.requested$	^$' ] || fail "operations_commands topic permissions: '$topic'"
echo "OK commands exchange, operations_commands limited to publishing orders.transition.requested"

echo "== accounts connect, the owner declares the command topology, the publisher stays confined =="
docker run --rm --network "${PROJ}_default" \
  -e RABBITMQ_ADMIN_USER -e RABBITMQ_ADMIN_PASSWORD -e STOREFRONT_MQ_PASSWORD -e OPERATIONS_MQ_PASSWORD \
  -e BRIDGE_MQ_PASSWORD -e OPERATIONS_COMMANDS_MQ_PASSWORD -e REQUIRE_COMMAND_PERMISSION_TESTS=1 \
  -v "$REPO/backend/orders/command_messaging.py:/smoke/command_messaging.py:ro" \
  -v "$REPO/services/order_operations/tests/test_command_permissions.py:/smoke/test_command_permissions.py:ro" \
  -w /smoke "$PY_IMAGE" sh -euc '
    pip install -q --disable-pip-version-check --root-user-action=ignore pika==1.4.4 pytest==9.1.1
    python - <<"PY"
import os
import urllib.request

import pika

import command_messaging

url = lambda user, secret, vhost: f"amqp://{user}:{os.environ[secret]}@rabbitmq:5672/{vhost}"
for user, secret, vhost in (("storefront_app", "STOREFRONT_MQ_PASSWORD", "storefront"),
                            ("operations_consumer", "OPERATIONS_MQ_PASSWORD", "operations"),
                            ("operations_bridge", "BRIDGE_MQ_PASSWORD", "storefront"),
                            ("operations_bridge", "BRIDGE_MQ_PASSWORD", "operations"),
                            ("operations_commands", "OPERATIONS_COMMANDS_MQ_PASSWORD", "storefront")):
    pika.BlockingConnection(pika.URLParameters(url(user, secret, vhost))).close()
    print(f"OK {user}@{vhost}")
owner = pika.BlockingConnection(pika.URLParameters(url("storefront_app", "STOREFRONT_MQ_PASSWORD", "storefront")))
command_messaging.declare_topology(owner.channel())
owner.close()
print("OK command topology declared by its owner")
body = urllib.request.urlopen("http://rabbitmq:15692/metrics", timeout=5).read().decode()
assert "rabbitmq_" in body, "rabbitmq_prometheus metrics not exposed"
print("OK rabbitmq_prometheus metrics")
PY
    export COMMANDS_MQ_URL_FOR_PERMISSIONS="amqp://operations_commands:${OPERATIONS_COMMANDS_MQ_PASSWORD}@rabbitmq:5672/storefront"
    export ADMIN_MQ_URL_FOR_PERMISSIONS="amqp://${RABBITMQ_ADMIN_USER}:${RABBITMQ_ADMIN_PASSWORD}@rabbitmq:5672/storefront"
    python -m pytest -q -p no:cacheprovider test_command_permissions.py
  '

echo "SMOKE OK"

#!/usr/bin/env bash
# production broker bootstrap in an isolated compose project: a bare broker, the one-shot
# rabbitmq-provision service run exactly as a release runs it, the ports each broker network
# reaches, then every grant it must leave behind and nothing else, stale grants taken back, a
# failed revoke reported, and the isolation of each account from the applications' network.
# reads only tracked config and a throwaway env-file outside the checkout, and tears down only
# its own project
set -euo pipefail

PROJ="${SMOKE_PROJECT:-ar_prodsmoke_$(date +%s)_$$}"
# the image media-init pins; this test only needs a python that can reach the broker
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
# on a runner the reason is also an annotation, which the public api shows without access to the job log
fail() { echo "FAIL: $*" >&2; [ -z "${GITHUB_ACTIONS:-}" ] || echo "::error::$*"; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

export RABBITMQ_ADMIN_USER=smoke_admin RABBITMQ_ADMIN_PASSWORD="$(rnd)"
export STOREFRONT_MQ_PASSWORD="$(rnd)" OPERATIONS_MQ_PASSWORD="$(rnd)" BRIDGE_MQ_PASSWORD="$(rnd)"
export OPERATIONS_COMMANDS_MQ_PASSWORD="$(rnd)"
printf 'throwaway\n' > "$WORK/identity.pem"
# the whole production render has to interpolate, although only the broker is started
cat > "$ENV_FILE" <<EOF
SOURCE_REVISION=$PROJ
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

echo "== broker ports from the networks of the applications and of prometheus =="
# the management api, epmd and erlang distribution answer only inside the broker, where provisioning just ran
for net in broker_storefront broker_operations metrics_broker; do
  seen="$(docker run --rm --network "${PROJ}_$net" "$PY_IMAGE" python -c "
import socket
row = []
for port in (5672, 15692, 15672, 4369, 25672):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(('rabbitmq', port))
        row.append(f'{port}:open')
    except OSError:
        row.append(f'{port}:closed')
    finally:
        s.close()
print(' '.join(row))")"
  [ "$seen" = "5672:open 15692:open 15672:closed 4369:closed 25672:closed" ] || fail "from $net the broker answers: $seen"
done
echo "OK only amqp and metrics reach out of the broker; management, epmd and distribution stay on its loopback"

echo "== grants left behind =="
q() { dc exec -T -u rabbitmq rabbitmq rabbitmqctl -q --no-table-headers "$@"; }
# every grant of every vhost, so a grant nobody asked for fails as surely as a missing one
grants() {
  for vhost in / storefront operations; do q list_permissions -p "$vhost" | sed "s|^|$vhost	|"; done | sort
  q list_topic_permissions -p storefront | sed 's|^|topic	|'
}
expected_grants="$(printf '%s\n' \
  "/	$RABBITMQ_ADMIN_USER	.*	.*	.*" \
  "operations	$RABBITMQ_ADMIN_USER	.*	.*	.*" \
  'operations	operations_bridge	^operations\.events$	^operations\.events$	^$' \
  'operations	operations_consumer	^operations\.	^operations\.	^operations\.' \
  "storefront	$RABBITMQ_ADMIN_USER	.*	.*	.*" \
  'storefront	operations_bridge	^operations\.bridge\.	^operations\.bridge\.	^(orders|operations\.bridge\.)' \
  'storefront	operations_commands	^$	^commands$	^$' \
  'storefront	storefront_app	.*	.*	.*' | sort
  printf '%s\n' 'topic	operations_commands	commands	^orders\.transition\.requested$	^$')"
[ "$(grants)" = "$expected_grants" ] || fail "grants differ from the model:
$(diff <(echo "$expected_grants") <(grants) || true)"
echo "OK every grant in every vhost is the one the model names, and none else"

echo "== a wider grant left from an earlier layout is taken back =="
# --no-table-headers is an option of the listing commands only, so the grants are written without q
grant() { dc exec -T -u rabbitmq rabbitmq rabbitmqctl -q set_permissions "$@" >/dev/null; }
grant -p operations storefront_app '.*' '.*' '.*'
grant -p storefront operations_bridge '.*' '^operations\.bridge\.' '^(orders|operations\.bridge\.)'
dc --profile provision run --rm rabbitmq-provision >/dev/null || fail "rabbitmq-provision over stale grants failed"
[ "$(grants)" = "$expected_grants" ] || fail "provisioning left stale grants:
$(diff <(echo "$expected_grants") <(grants) || true)"
echo "OK provisioning converges stale grants back to the model"

echo "== a revoke that fails stops the provisioning =="
mkdir "$WORK/shim"
printf '#!/bin/sh\nfor arg in "$@"; do\n  [ "$arg" = clear_permissions ] && exit 70\ndone\nexec /opt/rabbitmq/sbin/rabbitmqctl "$@"\n' > "$WORK/shim/rabbitmqctl"
chmod +x "$WORK/shim/rabbitmqctl"
if dc --profile provision run --rm -v "$WORK/shim:/shim:ro" --entrypoint bash rabbitmq-provision \
    -c 'PATH=/shim:$PATH bash /provision.sh' >/dev/null 2>&1; then
  fail "provisioning reported success although clearing a stale grant failed"
fi
echo "OK a failed revoke is not reported as a completed provisioning"
dc --profile provision run --rm rabbitmq-provision >/dev/null || fail "rabbitmq-provision after the failed run failed"
exchange="$(q list_exchanges -p storefront name type durable | grep "^commands	" || true)"
[ "$exchange" = "commands	topic	true" ] || fail "exchange commands: '$exchange'"
perms="$(q list_user_permissions operations_commands)"
[ "$perms" = 'storefront	^$	^commands$	^$' ] || fail "operations_commands permissions: '$perms'"
topic="$(q list_user_topic_permissions operations_commands)"
[ "$topic" = 'storefront	commands	^orders\.transition\.requested$	^$' ] || fail "operations_commands topic permissions: '$topic'"
echo "OK commands exchange, operations_commands limited to publishing orders.transition.requested"

echo "== accounts connect, the owner declares the command topology, the publisher stays confined =="
# the broker network has no route out, so the client packages are fetched beforehand by a container that has one; it
# writes as the invoking user, or the cleanup on a linux runner cannot remove files the docker daemon made as root
mkdir "$WORK/wheels"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$WORK/wheels:/wheels" "$PY_IMAGE" \
  pip download -q --disable-pip-version-check --no-cache-dir -d /wheels pika==1.4.4 pytest==9.1.1
# from the network the storefront processes use: what an application container can and cannot do there
docker run --rm --network "${PROJ}_broker_storefront" \
  -e RABBITMQ_ADMIN_USER -e RABBITMQ_ADMIN_PASSWORD -e STOREFRONT_MQ_PASSWORD -e OPERATIONS_MQ_PASSWORD \
  -e BRIDGE_MQ_PASSWORD -e OPERATIONS_COMMANDS_MQ_PASSWORD -e REQUIRE_COMMAND_PERMISSION_TESTS=1 \
  -e BROKER_ISOLATION_HOST=rabbitmq -e REQUIRE_BROKER_ISOLATION_TESTS=1 \
  -v "$REPO/backend/orders/command_messaging.py:/smoke/command_messaging.py:ro" \
  -v "$REPO/services/order_operations/tests/test_command_permissions.py:/smoke/test_command_permissions.py:ro" \
  -v "$REPO/services/order_operations/tests/test_broker_isolation.py:/smoke/test_broker_isolation.py:ro" \
  -v "$WORK/wheels:/wheels:ro" -w /smoke "$PY_IMAGE" sh -euc '
    pip install -q --disable-pip-version-check --root-user-action=ignore --no-index --find-links /wheels pika==1.4.4 pytest==9.1.1
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
    python -m pytest -q -p no:cacheprovider test_command_permissions.py test_broker_isolation.py
  '

echo "SMOKE OK"

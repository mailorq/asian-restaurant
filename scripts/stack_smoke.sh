#!/usr/bin/env bash
# the production render released as DEPLOY.md releases it, in an isolated compose project, then checked on the live processes:
# nginx without root behind a tls ingress, the photo upload at its limits, a recreated backend, prometheus without root
# keeping a tsdb volume written before its hardening, and the network model: who reaches whom and who has a route out
# reads only tracked config and a throwaway env-file outside the checkout, and tears down only its own project
set -euo pipefail

PROJ="${SMOKE_PROJECT:-ar_stack_$(date +%s)_$$}"
PY_IMAGE=python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
WORK="$(mktemp -d)"
REPO="$PWD"
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
  REPO="$(cygpath -m "$REPO")"
fi
ENV_FILE="$WORK/production.env"

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml -f "$WORK/smoke.yaml" "$@"; }
cleanup() {
  echo "== teardown (only $PROJ) =="
  docker rm -f "${PROJ}_placeholder" "${PROJ}_geocoder" >/dev/null 2>&1 || true
  dc --profile provision down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# the deviations from the production render: the smoke reaches the stack from its own containers and takes no host port,
# and the backend asks a local stand in for the geocoder
cat > "$WORK/smoke.yaml" <<'YAML'
services:
  frontend:
    ports: !reset []
  prometheus:
    ports: !reset []
  backend:
    environment:
      GEOCODER_URL: http://geocoder:8080/search
YAML
openssl genrsa -out "$WORK/identity.pem" 2048 2>/dev/null
# read inside the container as 10001
chmod 644 "$WORK/identity.pem"
STOREFRONT_MQ_PASSWORD="$(rnd)" OPERATIONS_MQ_PASSWORD="$(rnd)" BRIDGE_MQ_PASSWORD="$(rnd)" OPERATIONS_COMMANDS_MQ_PASSWORD="$(rnd)"
cat > "$ENV_FILE" <<EOF
POSTGRES_DB=storefront
POSTGRES_USER=smoke_boot
POSTGRES_PASSWORD=$(rnd)
STOREFRONT_DB_MIGRATOR_PASSWORD=$(rnd)
STOREFRONT_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=smoke_ops_boot
OPERATIONS_DB_PASSWORD=$(rnd)
OPERATIONS_DB_MIGRATOR_PASSWORD=$(rnd)
OPERATIONS_DB_RUNTIME_PASSWORD=$(rnd)
DJANGO_SECRET_KEY=smoke-$(rnd)
DJANGO_ALLOWED_HOSTS=smoke.invalid,backend
REDIS_URL=redis://redis:6379/1
CART_REDIS_URL=redis://redis:6379/0
RABBITMQ_URL=amqp://storefront_app:$STOREFRONT_MQ_PASSWORD@rabbitmq:5672/storefront
IDENTITY_JWT_KID=smoke-1
IDENTITY_JWT_KEY_FILE=$WORK/identity.pem
RABBITMQ_ADMIN_USER=smoke_admin
RABBITMQ_ADMIN_PASSWORD=$(rnd)
RABBITMQ_ERLANG_COOKIE=$(rnd)
STOREFRONT_MQ_PASSWORD=$STOREFRONT_MQ_PASSWORD
OPERATIONS_MQ_PASSWORD=$OPERATIONS_MQ_PASSWORD
BRIDGE_MQ_PASSWORD=$BRIDGE_MQ_PASSWORD
OPERATIONS_COMMANDS_MQ_PASSWORD=$OPERATIONS_COMMANDS_MQ_PASSWORD
OPERATIONS_SECRET_KEY=smoke-$(rnd)
OPERATIONS_ALLOWED_HOSTS=smoke.invalid,operations-api
OPERATIONS_RABBITMQ_URL=amqp://operations_consumer:$OPERATIONS_MQ_PASSWORD@rabbitmq:5672/operations
OPERATIONS_BRIDGE_CONSUME_URL=amqp://operations_bridge:$BRIDGE_MQ_PASSWORD@rabbitmq:5672/storefront
OPERATIONS_BRIDGE_PUBLISH_URL=amqp://operations_bridge:$BRIDGE_MQ_PASSWORD@rabbitmq:5672/operations
OPERATIONS_COMMANDS_RABBITMQ_URL=amqp://operations_commands:$OPERATIONS_COMMANDS_MQ_PASSWORD@rabbitmq:5672/storefront
EOF

cp scripts/stack_probe.py "$WORK/probe.py"
# from where the ingress stands, and from where an operator on the host reaches prometheus
probe() { docker run --rm --network "${PROJ}_public_frontend" -e SMOKE_ADMIN_PASSWORD -e SMOKE_EMPLOYEE_PASSWORD -v "$WORK/probe.py:/probe.py:ro" "$PY_IMAGE" python /probe.py "$@"; }
prom() { docker run --rm --network "${PROJ}_public_prometheus" -v "$WORK/probe.py:/probe.py:ro" "$PY_IMAGE" python /probe.py "$@"; }
until_ok() {  # until_ok <seconds> <cmd...>
  local deadline=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do [ "$SECONDS" -lt "$deadline" ] || return 1; sleep 2; done
}
# the backend address on the network nginx reaches it through
edge_address() { docker inspect -f "{{(index .NetworkSettings.Networks \"${PROJ}_edge\").IPAddress}}" "$(dc ps -q backend)"; }
confined() {  # confined <service> <a path on its root filesystem>
  local uid caps
  uid="$(dc exec -T "$1" id -u)"
  [ "$uid" != 0 ] || fail "$1 runs as root"
  caps="$(dc exec -T "$1" sh -c "sed -n 's/^CapEff:[[:space:]]*//p' /proc/1/status")"
  [ "$caps" = 0000000000000000 ] || fail "$1 keeps capabilities $caps"
  if dc exec -T "$1" sh -c "echo x > $2" >/dev/null 2>&1; then fail "$1 wrote $2 on its root filesystem"; fi
  echo "OK $1 runs as uid $uid with no capabilities on a read only root filesystem"
}

echo "== release, as DEPLOY.md runs it =="
dc build >"$WORK/build.log" 2>&1 || { tail -30 "$WORK/build.log"; fail "the images did not build"; }
dc up -d rabbitmq >/dev/null 2>&1
until_ok 180 dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q check_running || fail "the broker never started"
dc --profile provision run --rm rabbitmq-provision >/dev/null 2>&1 || fail "rabbitmq-provision failed"
dc up --exit-code-from storefront-migrate storefront-migrate >"$WORK/migrate.log" 2>&1 || { tail -20 "$WORK/migrate.log"; fail "storefront migrations failed"; }
dc up --exit-code-from operations-migrate operations-migrate >>"$WORK/migrate.log" 2>&1 || { tail -20 "$WORK/migrate.log"; fail "operations migrations failed"; }
# the volume a release before the prometheus hardening left behind
cat > "$WORK/before-hardening.yaml" <<'YAML'
services:
  prometheus:
    user: !reset null
    read_only: false
    cap_drop: !reset []
    security_opt: !reset []
    mem_limit: !reset null
    pids_limit: !reset null
YAML
dc -f "$WORK/before-hardening.yaml" up -d --no-deps prometheus >/dev/null 2>&1
until_ok 90 prom sample_time || fail "the prometheus before hardening never scraped itself"
before_hardening="$(prom sample_time)"
dc rm -sf prometheus >/dev/null 2>&1
dc up -d >"$WORK/up.log" 2>&1 || { tail -30 "$WORK/up.log"; fail "the runtime did not start"; }
docker run -d --name "${PROJ}_geocoder" --network "${PROJ}_egress_backend" --network-alias geocoder "$PY_IMAGE" python -c '
import http.server, json
class Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([{"lat": "55.75", "lon": "37.61", "display_name": "smoke"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("", 8080), Stub).serve_forever()
' >/dev/null
until_ok 120 probe health || fail "nginx never answered /api/health with 200"
echo "OK the release came up and nginx answers"

echo "== nginx =="
confined frontend /etc/nginx/probe
dc exec -T frontend nginx -t >/dev/null 2>&1 || fail "nginx -t fails as the runtime user"
dc exec -T backend sh -c 'echo "edge probe" > /app/media/edge-probe.txt'
problems="$(probe edge)"
[ "$problems" = "[]" ] || fail "edge: $problems"
echo "OK spa, api, admin, static and media answer through nginx with the security headers, redirects keep the client's https and no :8080"

echo "== photo upload through nginx at its limits =="
export SMOKE_ADMIN_PASSWORD="$(rnd)"
product="$(dc exec -T -e SMOKE_ADMIN_PASSWORD backend python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
from menu.models import Product
get_user_model().objects.create_superuser(username='+79990001234', password=os.environ['SMOKE_ADMIN_PASSWORD'])
print(Product.objects.create(code='edge_photo', category='dish', name='Фото', price='100.00').pk)
" | tail -1)"
problems="$(probe upload "$product")"
[ "$problems" = "[]" ] || fail "upload: $problems"
echo "OK a 5 mib photo is stored and served, one byte more is refused by the form, a body past 6 mib by nginx with 413, the api past 1 mib"
stats="$(docker stats --no-stream --format '{{.MemUsage}} pids {{.PIDs}}' "$(dc ps -q frontend)")"
echo "OK nginx after the uploads: $stats"

echo "== a recreated backend on a new address =="
before="$(edge_address)"
dc rm -sf backend >/dev/null 2>&1
# the freed address goes to another container, so the new backend cannot come back on it
docker run -d --name "${PROJ}_placeholder" --network "${PROJ}_edge" "$PY_IMAGE" sleep 600 >/dev/null
dc up -d --no-deps backend >/dev/null 2>&1
after="$(edge_address)"
[ "$before" != "$after" ] || fail "the backend came back on $after, the check proves nothing"
until_ok 60 probe health || fail "nginx does not reach the backend recreated on $after (was $before)"
echo "OK nginx reaches the backend recreated on $after (was $before)"

echo "== prometheus =="
confined prometheus /etc/prometheus/probe
problems="$(prom monitoring)"
[ "$problems" = "[]" ] || fail "prometheus: $problems"
prom has_sample "$before_hardening" || fail "the samples written before the hardening at $before_hardening are gone"
hardened="$(prom sample_time)"
dc up -d --force-recreate --no-deps prometheus >/dev/null 2>&1
until_ok 90 prom sample_time || fail "the recreated prometheus does not answer"
for at in "$before_hardening" "$hardened"; do
  prom has_sample "$at" || fail "the recreated prometheus lost the samples of $at"
done
stats="$(docker stats --no-stream --format '{{.MemUsage}} pids {{.PIDs}}' "$(dc ps -q prometheus)")"
echo "OK every target is up, config and rules are loaded, the tsdb takes writes, and it keeps what was written before the hardening and before a recreation: $stats"

echo "== order status from the panel, through operations, the broker and the storefront, and back =="
export SMOKE_EMPLOYEE_PASSWORD="$(rnd)"
seeded="$(dc exec -T -e SMOKE_EMPLOYEE_PASSWORD backend python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
from menu.models import Product
from orders.models import DeliveryAddress, Order, OrderItem, OrderStatusHistory
users = get_user_model().objects
secret = os.environ['SMOKE_EMPLOYEE_PASSWORD']
employee = users.create_user(username='+79990005678', phone='+79990005678', password=secret)
customer = users.create_user(username='+79990005679', phone='+79990005679', password=secret)
product = Product.objects.get(code='edge_photo')
address = DeliveryAddress.objects.create(user=customer, address='smoke street 1', is_verified=True)
orders = []
for n in range(3):
    order = Order.objects.create(user=customer, status='created', payment_method='cash', phone=customer.phone, contact_name='smoke',
                                 delivery_address=address, total='100.00', idempotency_key=f'smoke-order-{n}', source_cart_id=f'smoke-{n}',
                                 source_cart_version=1)
    OrderItem.objects.create(order=order, product=product, product_name=product.name, unit_price='100.00', quantity=1, line_total='100.00')
    OrderStatusHistory.objects.create(order=order, from_status='', to_status='created', note='smoke')
    orders.append(order.pk)
print(employee.pk, *orders)
" | tail -1)"
read -r employee order waits refused <<<"$seeded"
dc exec -T backend python manage.py grant_employee +79990005678 --role restaurant_manager --actor +79990001234 >/dev/null
dc exec -T backend python manage.py transitions_via_commands +79990005678 >/dev/null
known() { dc exec -T operations-api python manage.py shell -c "import sys; from operations.models import EmployeeAuthorization as E; sys.exit(0 if E.objects.filter(subject_id=$employee, role_active=True).exists() else 1)"; }
until_ok 120 known || fail "operations never learned the role of the employee"
problems="$(probe command_plane "$order" +79990005678)"
[ "$problems" = "[]" ] || fail "command plane: $problems"
echo "OK the panel's command confirmed the order through operations and the storefront, a repeat returned it, another intent under its key was refused, and the direct transition stayed closed"

echo "== the command plane without its broker and without its limiter store =="
dc stop rabbitmq >/dev/null 2>&1
(sleep 30; dc start rabbitmq >/dev/null 2>&1) &
outage="$(probe send_command "$waits" +79990005678 240)"
wait
grep -q '"status": 202' <<<"$outage" && grep -q '"pending"' <<<"$outage" && grep -q '"final": "succeeded"' <<<"$outage"   || fail "a command sent while the broker was down: $outage"
dc stop operations-redis >/dev/null 2>&1
refused_answer="$(probe send_command "$refused" +79990005678 0)"
dc start operations-redis >/dev/null 2>&1
grep -q '"status": 503' <<<"$refused_answer" || fail "a command without the limiter store: $refused_answer"
status="$(dc exec -T backend python manage.py shell -c "from orders.models import Order; print(Order.objects.get(pk=$refused).status)" | tail -1)"
[ "$status" = created ] || fail "the order refused for want of the limiter store moved to $status"
echo "OK a command sent while the broker was down waited for it and was applied, and without the limiter store a new command was refused with 503 and the order stayed"

echo "== networks =="
# tcp from inside a container: python where the image has it, busybox nc elsewhere
# stdin stays with the loops that read the lists below; exec would swallow the rest of them
reach() { dc exec -T "$1" sh -c 'if command -v python >/dev/null; then python -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 3)" "$0" "$1"; else nc -z -w 3 "$0" "$1"; fi' "$2" "$3" </dev/null >/dev/null 2>&1; }
# a default route in the kernel table is the only way out of the host's docker networks
routed_out() { dc exec -T "$1" awk 'NR > 1 && $2 == "00000000" { found = 1 } END { exit !found }' /proc/net/route </dev/null; }
# a cut flow only counts where the probe itself works
for svc in frontend backend relay commands-consumer operations-api operations-consumer operations-bridge commands-relay prometheus rabbitmq; do
  dc exec -T "$svc" sh -c 'command -v python || command -v nc' >/dev/null || fail "$svc has neither python nor nc to probe with"
done
reach rabbitmq rabbitmq 5672 || fail "the probe does not work inside rabbitmq"
denied=()
checked=0
while read -r from to port; do
  reach "$from" "$to" "$port" || fail "$from cannot reach $to:$port, a flow of the model"
  checked=$((checked + 1))
done <<'FLOWS'
frontend backend 8000
backend db 5432
backend redis 6379
backend rabbitmq 5672
backend operations-api 9000
backend geocoder 8080
relay db 5432
relay rabbitmq 5672
commands-consumer db 5432
commands-consumer rabbitmq 5672
operations-api operations-db 5432
operations-api operations-redis 6379
operations-api backend 8000
operations-consumer operations-db 5432
operations-consumer rabbitmq 5672
operations-bridge operations-db 5432
operations-bridge rabbitmq 5672
commands-relay operations-db 5432
commands-relay rabbitmq 5672
prometheus backend 8000
prometheus rabbitmq 15692
FLOWS
while read -r from to port; do
  if reach "$from" "$to" "$port"; then denied+=("$from->$to:$port"); fi
  checked=$((checked + 1))
done <<'CUT'
frontend db 5432
frontend redis 6379
frontend rabbitmq 5672
frontend operations-api 9000
frontend prometheus 9090
backend operations-db 5432
backend operations-redis 6379
relay redis 6379
relay operations-api 9000
relay geocoder 8080
commands-consumer operations-db 5432
operations-api db 5432
operations-api redis 6379
operations-api rabbitmq 5672
operations-api geocoder 8080
operations-consumer backend 8000
operations-consumer db 5432
operations-consumer relay 9104
operations-bridge commands-consumer 9102
commands-relay backend 8000
prometheus db 5432
prometheus operations-db 5432
prometheus redis 6379
prometheus operations-redis 6379
prometheus geocoder 8080
rabbitmq db 5432
rabbitmq operations-api 9000
CUT
[ "${#denied[@]}" = 0 ] || fail "reachable against the model: ${denied[*]}"
[ "$checked" = 48 ] || fail "only $checked of the 48 listed connections were checked"
for svc in backend frontend prometheus; do
  routed_out "$svc" || fail "$svc has no route out, the model gives it one"
done
for svc in db redis rabbitmq relay commands-consumer operations-db operations-redis operations-api operations-consumer operations-bridge commands-relay; do
  if routed_out "$svc"; then fail "$svc has a route out of the host"; fi
done
found="$(dc exec -T backend python manage.py shell -c "from orders.geocode import geocode; print(geocode('smoke street 1').found)" | tail -1)"
[ "$found" = True ] || fail "the backend geocoder call did not come back: $found"
echo "OK every flow of the model connects, the cut ones do not, only backend, frontend and prometheus have a route out, and the geocoder call goes out"

echo "SMOKE OK"

#!/usr/bin/env bash
# the storefront under load on the production render, in an isolated compose project
# first the stock invariant: buyers check out the same product at once through the order service in the backend image,
# each on its own database connection, and the orders that went through must account for every unit that left the stock
# then http through nginx, as the tls ingress hands it on: each simulated client under its own forwarded address. the
# run prints the conditions next to the numbers; latency is reported, never gated. a 5xx, a limit that does not hold or
# a count that does not add up fails it
# reads only tracked config and a throwaway env-file outside the checkout, and tears down only its own project
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/disposable_project.sh"

PROJ="ar_load_$(date +%s)_$$"
claim_project "$PROJ"
PY_IMAGE=python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
WORK="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
fi
ENV_FILE="$WORK/production.env"

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml -f "$WORK/load.yaml" "$@"; }
cleanup() {
  local status=$?
  echo "== teardown (only $PROJ) =="
  dc --profile provision down -v --remove-orphans >/dev/null 2>&1 || true
  docker image rm "asian-restaurant/storefront:$PROJ" "asian-restaurant/operations:$PROJ" "asian-restaurant/frontend:$PROJ" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  if ! teardown_clean "$PROJ"; then [ "$status" -ne 0 ] || status=1; fi
  exit "$status"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; [ -z "${GITHUB_ACTIONS:-}" ] || echo "::error::$*"; exit 1; }
ok() { echo "OK $*"; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
until_ok() {  # until_ok <seconds> <cmd...>
  local deadline=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do [ "$SECONDS" -lt "$deadline" ] || return 1; sleep 2; done
}

# the deviations from the production render: no host port, and no geocoder to reach, so an address stays unverified
cat > "$WORK/load.yaml" <<'YAML'
services:
  frontend:
    ports: !reset []
  prometheus:
    ports: !reset []
  backend:
    environment:
      GEOCODER_ENABLED: "0"
YAML
openssl genrsa -out "$WORK/identity.pem" 2048 2>/dev/null
# read inside the container as 10001
chmod 644 "$WORK/identity.pem"
SF_MQ="$(rnd)" OPS_MQ="$(rnd)" BRIDGE_MQ="$(rnd)" COMMANDS_MQ="$(rnd)"
export LOAD_PASSWORD="load-$(rnd)"
cat > "$ENV_FILE" <<EOF
SOURCE_REVISION=$PROJ
POSTGRES_DB=storefront
POSTGRES_USER=load_boot
POSTGRES_PASSWORD=$(rnd)
STOREFRONT_DB_MIGRATOR_PASSWORD=$(rnd)
STOREFRONT_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=load_ops_boot
OPERATIONS_DB_PASSWORD=$(rnd)
OPERATIONS_DB_MIGRATOR_PASSWORD=$(rnd)
OPERATIONS_DB_RUNTIME_PASSWORD=$(rnd)
DJANGO_SECRET_KEY=load-$(rnd)$(rnd)
DJANGO_ALLOWED_HOSTS=load.invalid,backend
REDIS_URL=redis://redis:6379/1
CART_REDIS_URL=redis://redis:6379/0
RABBITMQ_URL=amqp://storefront_app:$SF_MQ@rabbitmq:5672/storefront
IDENTITY_JWT_KID=load-1
IDENTITY_JWT_KEY_FILE=$WORK/identity.pem
RABBITMQ_ADMIN_USER=load_admin
RABBITMQ_ADMIN_PASSWORD=$(rnd)
RABBITMQ_ERLANG_COOKIE=$(rnd)
STOREFRONT_MQ_PASSWORD=$SF_MQ
OPERATIONS_MQ_PASSWORD=$OPS_MQ
BRIDGE_MQ_PASSWORD=$BRIDGE_MQ
OPERATIONS_COMMANDS_MQ_PASSWORD=$COMMANDS_MQ
OPERATIONS_SECRET_KEY=load-$(rnd)$(rnd)
OPERATIONS_ALLOWED_HOSTS=load.invalid,operations-api
OPERATIONS_RABBITMQ_URL=amqp://operations_consumer:$OPS_MQ@rabbitmq:5672/operations
OPERATIONS_BRIDGE_CONSUME_URL=amqp://operations_bridge:$BRIDGE_MQ@rabbitmq:5672/storefront
OPERATIONS_BRIDGE_PUBLISH_URL=amqp://operations_bridge:$BRIDGE_MQ@rabbitmq:5672/operations
OPERATIONS_COMMANDS_RABBITMQ_URL=amqp://operations_commands:$COMMANDS_MQ@rabbitmq:5672/storefront
EOF
cp scripts/load_probe.py "$WORK/probe.py"
# the probe exits non zero when it found a problem, and prints what it measured either way
probe() { docker run --rm --network "${PROJ}_public_frontend" -e LOAD_PASSWORD -v "$WORK/probe.py:/probe.py:ro" "$PY_IMAGE" python /probe.py "$@"; }
shell() { dc exec -T "$@" backend python manage.py shell; }
result() { grep '^RESULT ' | tail -1 | cut -c8-; }
problems() {  # problems <json>: what the probe found wrong, or that it printed nothing it could be judged by
  python3 -c 'import json, sys
raw = sys.stdin.read()
try:
    print("; ".join(json.loads(raw)["problems"]))
except Exception:
    print("unreadable output: " + raw[:300])' <<<"$1"
}
field() { python3 -c 'import json, sys; d = json.load(sys.stdin)
for k in sys.argv[1].split("."): d = d[k]
print(d)' "$2" <<<"$1"; }

echo "== release of the production render, storefront only =="
dc build backend frontend >"$WORK/build.log" 2>&1 || { tail -30 "$WORK/build.log"; fail "the images did not build"; }
dc up -d rabbitmq >/dev/null 2>&1
until_ok 180 dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q check_running || fail "the broker never started"
dc --profile provision run --rm rabbitmq-provision >/dev/null 2>&1 || fail "rabbitmq-provision failed"
dc up -d frontend >"$WORK/up.log" 2>&1 || { tail -30 "$WORK/up.log"; fail "the storefront did not start"; }
until_ok 120 probe menu '{"clients": 1, "requests": 1}' || fail "nginx never served the catalog"
echo "conditions: backend $(docker inspect -f '{{join .Config.Cmd " "}}' "$(dc ps -q backend)"), database pool $(dc exec -T backend printenv DJANGO_DB_POOL_MAX_SIZE) per worker"

echo "== the stock invariant: buyers at once through the order service =="
stock_round() {  # stock_round <name> <buyers> <stock> <units each>
  shell -e ROUND="$1" -e BUYERS="$2" -e STOCK="$3" -e EACH="$4" -e DJANGO_DB_POOL_MAX_SIZE="$(($2 + 4))" <<'PY' | result
import asyncio, json, os, threading, time
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import F, Sum
from cart import service as cart_service
from menu.models import Product, StockAdjustment
from orders import service as order_service
from orders.models import OrderItem

name, buyers, stock, each = os.environ["ROUND"], int(os.environ["BUYERS"]), int(os.environ["STOCK"]), int(os.environ["EACH"])
product = Product.objects.create(code=f"load_{name}", category="dish", name=f"Нагрузка {name}", price="10.00",
                                 stock_quantity=stock)
people = [get_user_model().objects.create_user(username=f"load_{name}_{n}", phone="+380671111111", first_name="Покупатель")
          for n in range(buyers)]
for person in people:
    asyncio.run(cart_service.add(cart_service.user_key(person.id), product.id, each, None))
connection.close()
gate, answers, lock = threading.Barrier(buyers), [], threading.Lock()

def buy(person, n):
    gate.wait()
    try:
        order_service.checkout(person, "ул. Нагрузочная, 1", "cash", f"load-{name}-{n}")
        answer = "ordered"
    except order_service.CheckoutError as exc:
        answer = f"refused {exc.code}"
    except Exception as exc:
        answer = f"error {type(exc).__name__}: {exc}"[:200]
    finally:
        connection.close()
    with lock:
        answers.append(answer)

threads = [threading.Thread(target=buy, args=(person, n)) for n, person in enumerate(people)]
started = time.perf_counter()
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(120)
wall = time.perf_counter() - started
product.refresh_from_db()
sold = OrderItem.objects.filter(product=product).aggregate(units=Sum("quantity"))["units"] or 0
left = StockAdjustment.objects.filter(product=product, source="sale").aggregate(units=Sum(F("old_quantity") - F("new_quantity")))["units"] or 0
counts = {}
for answer in answers:
    counts[answer] = counts.get(answer, 0) + 1
print("RESULT " + json.dumps({
    "answers": counts, "orders": OrderItem.objects.filter(product=product).values("order").distinct().count(),
    "units_sold": sold, "units_left_by_sales": left, "stock": product.stock_quantity, "version": product.version,
    "seconds": round(wall, 2),
}, ensure_ascii=False))
connection.close()
PY
}
check_round() {  # check_round <name> <buyers> <stock> <units each>
  local out expect
  out="$(stock_round "$@")"
  [ -n "$out" ] || fail "round $1 printed no result"
  echo "round $1: $2 buyers of $4 unit(s) each for a stock of $3: $out"
  expect=$(($3 / $4))
  [ "$expect" -le "$2" ] || expect=$2
  [ "$(field "$out" orders)" = "$expect" ] || fail "round $1: $(field "$out" orders) orders, expected $expect"
  [ "$(field "$out" answers.ordered)" = "$expect" ] || fail "round $1: the service reported $(field "$out" answers.ordered) orders"
  [ "$(python3 -c 'import json, sys; a = json.load(sys.stdin)["answers"]; print(sum(v for k, v in a.items() if k not in ("ordered", "refused cart_changed")))' <<<"$out")" = 0 ] \
    || fail "round $1: answers other than an order or a refusal for the stock"
  [ "$(field "$out" units_sold)" = "$((expect * $4))" ] || fail "round $1: $(field "$out" units_sold) units in orders"
  [ "$(field "$out" units_left_by_sales)" = "$((expect * $4))" ] || fail "round $1: sales took $(field "$out" units_left_by_sales) units from the stock"
  [ "$(field "$out" stock)" = "$(($3 - expect * $4))" ] || fail "round $1: $(field "$out" stock) left in stock"
  [ "$(field "$out" version)" = "$((1 + expect))" ] || fail "round $1: version $(field "$out" version) after $expect sales"
}
check_round one 50 20 1
check_round three 50 20 3
ok "buyers at once never take more than the stock: every order is in the stock history and the rest is refused"

echo "== http through nginx, each client under its own forwarded address =="
seed="$(shell -e LOAD_PASSWORD <<'PY' | result
import json, os
from django.contrib.auth import get_user_model
from menu.models import Product

users = get_user_model()
for n in range(40):
    users.objects.create_user(username=f"load_http_{n}", password=os.environ["LOAD_PASSWORD"], phone="+380671111111")
users.objects.create_user(username="load_limits", password=os.environ["LOAD_PASSWORD"], phone="+380671111111")
burst = Product.objects.create(code="load_http", category="dish", name="Нагрузка http", price="10.00", stock_quantity=25)
limits = Product.objects.create(code="load_limits", category="dish", name="Нагрузка лимит", price="10.00", stock_quantity=100)
print("RESULT " + json.dumps({"burst": burst.id, "limits": limits.id}))
PY
)" || true
[ -n "$seed" ] || fail "the http buyers were not created"

out="$(probe menu '{"clients": 20, "requests": 1000}')" || true
echo "catalog: $out"
[ -z "$(problems "$out")" ] || fail "catalog: $(problems "$out")"

logins="$(python3 -c 'import json; print(json.dumps([f"load_http_{n}" for n in range(40)]))')"
out="$(probe checkout "{\"product_id\": $(field "$seed" burst), \"logins\": $logins}")" || true
echo "checkout burst: $out"
[ -z "$(problems "$out")" ] || fail "checkout burst: $(problems "$out")"
[ "$(field "$out" ordered)" = 25 ] || fail "checkout burst: $(field "$out" ordered) orders for a stock of 25"
left="$(shell <<<'from menu.models import Product; print("RESULT " + str(Product.objects.get(code="load_http").stock_quantity))' | result)"
[ "$left" = 0 ] || fail "checkout burst: $left left in stock after 25 orders"

out="$(probe limits "{\"product_id\": $(field "$seed" limits), \"login\": \"load_limits\"}")" || true
echo "limits: $out"
[ -z "$(problems "$out")" ] || fail "limits: $(problems "$out")"
ok "the catalog and checkout answer without 5xx, the burst sells exactly the stock and every limit refuses past its count"
echo "LOAD OK"

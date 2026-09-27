#!/usr/bin/env bash
# a restore rehearsal on disposable stands: a released production render with a seeded catalog, a customer order and its
# projection is backed up (both databases with pg_dump, the media volume with tar), then restored into a second, empty
# project the way DEPLOY.md describes it, released there and checked: the same rows, the same media bytes served by nginx,
# migrations at head, and a fresh snapshot run that operations reconciles without a discrepancy
# reads only tracked config and throwaway files outside the checkout, and tears down only its own two projects
set -euo pipefail

STAMP="$(date +%s)_$$"
SOURCE="ar_backup_$STAMP"
TARGET="ar_restore_$STAMP"
PY_IMAGE=python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
WORK="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
fi
ENV_FILE="$WORK/production.env"
PROJ="$SOURCE"

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml -f "$WORK/stand.yaml" "$@"; }
cleanup() {
  echo "== teardown (only $SOURCE and $TARGET) =="
  for PROJ in "$SOURCE" "$TARGET"; do dc --profile provision down -v --remove-orphans >/dev/null 2>&1 || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
until_ok() {  # until_ok <seconds> <cmd...>
  local deadline=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do [ "$SECONDS" -lt "$deadline" ] || return 1; sleep 3; done
}

# the stand takes no host port; nothing else differs from the production render
cat > "$WORK/stand.yaml" <<'YAML'
services:
  frontend:
    ports: !reset []
  prometheus:
    ports: !reset []
YAML
openssl genrsa -out "$WORK/identity.pem" 2048 2>/dev/null
chmod 644 "$WORK/identity.pem"
SF_BOOT=smoke_boot SF_BOOT_PW="$(rnd)" OPS_BOOT=smoke_ops_boot OPS_BOOT_PW="$(rnd)"
STOREFRONT_MQ_PASSWORD="$(rnd)" OPERATIONS_MQ_PASSWORD="$(rnd)" BRIDGE_MQ_PASSWORD="$(rnd)" OPERATIONS_COMMANDS_MQ_PASSWORD="$(rnd)"
# one secrets file for both stands, as a restore on a new host takes the secrets of the old one
cat > "$ENV_FILE" <<EOF
POSTGRES_DB=storefront
POSTGRES_USER=$SF_BOOT
POSTGRES_PASSWORD=$SF_BOOT_PW
STOREFRONT_DB_MIGRATOR_PASSWORD=$(rnd)
STOREFRONT_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=$OPS_BOOT
OPERATIONS_DB_PASSWORD=$OPS_BOOT_PW
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

release() {
  dc up -d rabbitmq >/dev/null 2>&1
  until_ok 180 dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q check_running || fail "$PROJ: the broker never started"
  dc --profile provision run --rm rabbitmq-provision >/dev/null 2>&1 || fail "$PROJ: rabbitmq-provision failed"
  dc up --exit-code-from storefront-migrate storefront-migrate >"$WORK/$PROJ-migrate.log" 2>&1 || { tail -20 "$WORK/$PROJ-migrate.log"; fail "$PROJ: storefront migrations failed"; }
  dc up --exit-code-from operations-migrate operations-migrate >>"$WORK/$PROJ-migrate.log" 2>&1 || { tail -20 "$WORK/$PROJ-migrate.log"; fail "$PROJ: operations migrations failed"; }
  dc up -d >"$WORK/$PROJ-up.log" 2>&1 || { tail -30 "$WORK/$PROJ-up.log"; fail "$PROJ: the runtime did not start"; }
}
# what has to come back: rows on both sides and the bytes of every media file
state() {
  dc exec -T backend python manage.py shell -c "
import json
from django.contrib.auth import get_user_model
from menu.models import Product
from orders.models import Order, OrderItem, OrderStatusHistory
print(json.dumps({'products': Product.objects.count(), 'stock': sum(Product.objects.values_list('stock_quantity', flat=True)),
                  'users': get_user_model().objects.count(), 'orders': Order.objects.count(), 'items': OrderItem.objects.count(),
                  'history': OrderStatusHistory.objects.count()}, sort_keys=True))" | tail -1
  dc exec -T operations-api python manage.py shell -c "
import json
from operations.models import CustomerProjection, InboxEvent, InventoryProjection, OperationOrder
print(json.dumps({'orders': OperationOrder.objects.count(), 'customers': CustomerProjection.objects.count(),
                  'products': InventoryProjection.objects.count(), 'inbox': InboxEvent.objects.count()}, sort_keys=True))" | tail -1
  docker run --rm -v "${PROJ}_media:/media:ro" "$PY_IMAGE" python -c "
import hashlib, pathlib
for f in sorted(p for p in pathlib.Path('/media').rglob('*') if p.is_file()):
    print(f.relative_to('/media'), hashlib.sha256(f.read_bytes()).hexdigest())"
}
served() {  # served <path>: the status and the sha256 of what nginx answers for it, as the ingress asks
  docker run --rm --network "${PROJ}_public_frontend" "$PY_IMAGE" python -c "
import hashlib, http.client, sys
c = http.client.HTTPConnection('frontend', 8080, timeout=10)
c.request('GET', sys.argv[1], headers={'Host': 'smoke.invalid', 'X-Forwarded-Proto': 'https'})
r = c.getresponse()
print(r.status, hashlib.sha256(r.read()).hexdigest())" "$1"
}

echo "== the source stand: released, seeded, projected =="
dc build >"$WORK/build.log" 2>&1 || { tail -30 "$WORK/build.log"; fail "the images did not build"; }
release
dc exec -T backend python manage.py seed_menu >/dev/null
dc exec -T backend python manage.py seed_initial_stock --confirm --quantity 50 >/dev/null
dc exec -T backend python manage.py shell -c "
from django.contrib.auth import get_user_model
from accounts import service as accounts_service
from menu.models import Product
from orders import service as order_service
from orders.models import DeliveryAddress, Order, OrderItem, OrderStatusHistory
customer = get_user_model().objects.create_user(username='+79990007001', phone='+79990007001', first_name='Restore')
product = Product.objects.order_by('code').first()
address = DeliveryAddress.objects.create(user=customer, address='restore street 1', is_verified=True)
order = Order.objects.create(user=customer, status='created', payment_method='card', phone=customer.phone, contact_name='Restore',
                             delivery_address=address, total=product.price, idempotency_key='restore-order', source_cart_id='restore',
                             source_cart_version=1)
OrderItem.objects.create(order=order, product=product, product_name=product.name, unit_price=product.price, quantity=1,
                         line_total=product.price)
OrderStatusHistory.objects.create(order=order, from_status='', to_status='created', note='restore')
accounts_service.emit_customer_created(customer)
order_service.emit_order_state(order)
" >/dev/null
# the seed photos are not tracked, so the stand writes a media file of its own that has to come back byte for byte
dc exec -T backend sh -c 'mkdir -p /app/media/products && head -c 65536 /dev/urandom > /app/media/products/restore-probe.bin'
projected() { dc exec -T operations-api python manage.py shell -c "import sys; from operations.models import OperationOrder; sys.exit(0 if OperationOrder.objects.exists() else 1)"; }
until_ok 120 projected || fail "the order never reached the operations projection"
# the backup is compared with this state, so nothing may still be on its way: the outbox is drained and operations took the last event
drained() { dc exec -T backend python manage.py shell -c "import sys; from orders.models import OrderOutbox; sys.exit(0 if not OrderOutbox.objects.exclude(status='published').exists() else 1)"; }
inbox() { dc exec -T operations-api python manage.py shell -c "from operations.models import InboxEvent; print(InboxEvent.objects.count())" | tail -1; }
until_ok 120 drained || fail "the storefront outbox never drained"
quiet() { local seen; seen="$(inbox)"; sleep 5; [ "$seen" = "$(inbox)" ]; }
until_ok 120 quiet || fail "operations kept taking events"
before="$(state)"
photo=/media/products/restore-probe.bin
echo "OK the source holds $(head -1 <<<"$before") and $(sed -n 2p <<<"$before"), $(grep -c '^products/' <<<"$before") media files"

echo "== backup, as a scheduled job would take it from the running stand =="
dc exec -T db pg_dump -U "$SF_BOOT" -d storefront -Fc >"$WORK/storefront.dump"
dc exec -T operations-db pg_dump -U "$OPS_BOOT" -d operations -Fc >"$WORK/operations.dump"
docker run --rm -v "${PROJ}_media:/media:ro" "$PY_IMAGE" tar -C /media -cf - . >"$WORK/media.tar"
echo "OK storefront $(wc -c <"$WORK/storefront.dump") bytes, operations $(wc -c <"$WORK/operations.dump") bytes, media $(wc -c <"$WORK/media.tar") bytes"
dc --profile provision down -v --remove-orphans >/dev/null 2>&1

echo "== restore into an empty stand =="
PROJ="$TARGET"
dc up -d db operations-db >/dev/null 2>&1
until_ok 60 dc exec -T db pg_isready -q || fail "the restored db never started"
until_ok 60 dc exec -T operations-db pg_isready -q || fail "the restored operations-db never started"
# roles first, so the restored objects are owned by the migrator and the runtime role gets its grants
dc run --rm storefront-db-provision >/dev/null 2>&1 || fail "storefront roles were not provisioned"
dc run --rm operations-db-provision >/dev/null 2>&1 || fail "operations roles were not provisioned"
dc exec -T db pg_restore -U "$SF_BOOT" -d storefront --no-owner --role=storefront_migrator --exit-on-error <"$WORK/storefront.dump" \
  || fail "the storefront dump did not restore"
dc exec -T operations-db pg_restore -U "$OPS_BOOT" -d operations --no-owner --role=operations_migrator --exit-on-error <"$WORK/operations.dump" \
  || fail "the operations dump did not restore"
dc create media-init >/dev/null 2>&1
docker run --rm -i -v "${PROJ}_media:/media" "$PY_IMAGE" tar -C /media -xf - <"$WORK/media.tar"
dc run --rm --no-deps media-init >/dev/null 2>&1 || fail "media-init did not hand the restored media to the runtime user"
release
echo "OK restored and released: the migration jobs found both schemas at head"

echo "== what came back =="
healthy() { [ "$(served /api/health | cut -d' ' -f1)" = 200 ]; }
until_ok 120 healthy || fail "the restored stand does not answer through nginx"
after="$(state)"
[ "$before" = "$after" ] || { diff <(echo "$before") <(echo "$after") || true; fail "the restored stand differs from the source"; }
expected="$(grep -m1 "^${photo#/media/} " <<<"$before" | cut -d' ' -f2)"
[ "$(served "$photo")" = "200 $expected" ] || fail "nginx does not serve $photo as it was backed up"
echo "OK the same rows on both sides and the same media bytes, and nginx serves $photo as it was"
dc exec -T backend python manage.py emit_source_state --snapshot >"$WORK/snapshot.log"
run="$(grep -o 'snapshot run [0-9a-f]*' "$WORK/snapshot.log" | cut -d' ' -f3)"
[ -n "$run" ] || fail "no snapshot run was emitted"
until_ok 180 dc exec -T operations-api python manage.py reconcile --run-id "$run" || {
  dc exec -T operations-api python manage.py reconcile --run-id "$run" | tail -20
  fail "operations does not reconcile the restored storefront"
}
echo "OK a snapshot run $run taken after the restore reconciles without a discrepancy"

echo "REHEARSAL OK"

#!/usr/bin/env bash
# a restore rehearsal on disposable stands: a released production render with a seeded catalog and a projected order is
# backed up in the maintenance window DEPLOY.md describes, while a write is attempted between the two database dumps, then
# restored into a second, empty project and checked: the content of every table and sequence of both databases and the
# bytes of every media file, the photo nginx serves, and a fresh snapshot run that operations reconciles
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
# the canonical content of every table and the value of every sequence, read by the bootstrap superuser
cat > "$WORK/content.sql" <<'SQL'
select c.relname || ' ' || (xpath('/row/h/text()', query_to_xml(format(
         'select md5(coalesce(string_agg(t::text, E''\n'' order by t::text), '''')) as h from public.%I t', c.relname),
         false, true, '')))[1]::text
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'r'
 order by c.relname;
select 'sequence ' || sequencename || ' ' || coalesce(last_value::text, 'unused')
  from pg_sequences where schemaname = 'public' order by sequencename;
SQL
content() {  # content <database service> <user> <database>
  dc exec -T "$1" psql -X -A -t -q -U "$2" -d "$3" <"$WORK/content.sql"
}
media() {
  dc run --rm --no-deps -T --entrypoint python media-init -c "
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
# nothing is on its way to a database: no unpublished outbox row, no command without its outcome, no message waiting in a
# consumed or retry queue; dead letter queues are parked and change nothing
delivered() {
  local storefront operations queued
  storefront="$(dc exec -T relay python manage.py shell -c "from orders.models import OrderOutbox as O; print(O.objects.filter(status='pending').count())" | tail -1)"
  operations="$(dc exec -T operations-consumer python manage.py shell -c "from operations.models import OperationCommand as C, OperationsOutbox as O; print(O.objects.filter(status='pending').count() + C.objects.filter(status__in=['pending', 'dispatched']).count())" | tail -1)"
  queued="$(for vhost in storefront operations; do dc exec -T -u rabbitmq rabbitmq rabbitmqctl -q --no-table-headers list_queues -p "$vhost" name messages consumers; done \
    | awk '($3 > 0 || $1 ~ /\.retry$/) && $2 > 0' | wc -l)"
  [ "$storefront" = 0 ] && [ "$operations" = 0 ] && [ "$queued" = 0 ]
}
window() {
  dc stop frontend backend operations-api >/dev/null 2>&1
  until_ok 300 delivered || fail "the background processes never delivered what was in flight"
  dc stop relay commands-consumer commands-relay operations-consumer operations-bridge >/dev/null 2>&1
}
# the application confirming the order; while the window is closed there is nothing left to do it
write_between_dumps() {
  dc exec -T backend python manage.py shell -c "
from orders import service
from orders.models import Order
service.transition(Order.objects.get(idempotency_key='restore-order'), 'confirmed', expected_status='created', note='between the dumps')" >/dev/null 2>&1
}
confirmed_in_operations() {
  dc exec -T operations-consumer python manage.py shell -c "import sys; from operations.models import OperationOrder; sys.exit(0 if OperationOrder.objects.filter(status='confirmed').exists() else 1)"
}
# a copy taken without writes leaves every projection exactly where its source stands; reconcile takes a projection
# ahead of a snapshot for a later live event, so a copy with a write between its dumps passes it and only this catches it
sources() {
  dc exec -T backend python manage.py shell -c "
from django.db.models import Count
from menu.models import Product
from orders.models import Order
for o in Order.objects.annotate(v=Count('history')).order_by('id'): print('order', o.id, o.status, o.v)
for p in Product.objects.order_by('code'): print('product', p.code, p.stock_quantity, p.version)" | grep -E '^(order|product) '
}
projections() {
  dc exec -T operations-api python manage.py shell -c "
from operations.models import InventoryProjection, OperationOrder
for o in OperationOrder.objects.order_by('source_order_id'): print('order', o.source_order_id, o.status, o.aggregate_version)
for p in InventoryProjection.objects.order_by('product_code'): print('product', p.product_code, p.stock_quantity, p.aggregate_version)" | grep -E '^(order|product) '
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
projected() { dc exec -T operations-api python manage.py shell -c "import sys; from operations.models import OperationOrder; sys.exit(0 if OperationOrder.objects.exists() else 1)"; }
until_ok 120 projected || fail "the order never reached the operations projection"
echo "OK the source holds the seeded catalog and an order projected into operations"

echo "== backup in the maintenance window, with a write attempted between the dumps =="
window
storefront_before="$(content db "$SF_BOOT" storefront)"
dc exec -T db pg_dump -U "$SF_BOOT" -d storefront -Fc >"$WORK/storefront.dump"
if write_between_dumps; then
  echo "a write landed between the dumps"
  until_ok 120 confirmed_in_operations || true
else
  echo "OK the write attempted between the dumps found no process to make it"
fi
operations_before="$(content operations-db "$OPS_BOOT" operations)"
dc exec -T operations-db pg_dump -U "$OPS_BOOT" -d operations -Fc >"$WORK/operations.dump"
media_before="$(media)"
dc run --rm --no-deps -T --entrypoint tar media-init -cf - -C /media . >"$WORK/media.tar"
echo "OK storefront $(wc -c <"$WORK/storefront.dump") bytes, operations $(wc -c <"$WORK/operations.dump") bytes, media $(wc -c <"$WORK/media.tar") bytes"
dc --profile provision down -v --remove-orphans >/dev/null 2>&1

echo "== restore into an empty stand =="
PROJ="$TARGET"
dc up -d db operations-db >/dev/null 2>&1
until_ok 60 dc exec -T db sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' || fail "the restored db never started"
until_ok 60 dc exec -T operations-db sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' || fail "the restored operations-db never started"
# roles first, so the restored objects are owned by the migrator and the runtime role gets its grants
dc run --rm storefront-db-provision >/dev/null 2>&1 || fail "storefront roles were not provisioned"
dc run --rm operations-db-provision >/dev/null 2>&1 || fail "operations roles were not provisioned"
dc exec -T db pg_restore -U "$SF_BOOT" -d storefront --no-owner --role=storefront_migrator --exit-on-error <"$WORK/storefront.dump" \
  || fail "the storefront dump did not restore"
dc exec -T operations-db pg_restore -U "$OPS_BOOT" -d operations --no-owner --role=operations_migrator --exit-on-error <"$WORK/operations.dump" \
  || fail "the operations dump did not restore"
dc run --rm --no-deps -T --entrypoint tar media-init -xf - -C /media --no-same-owner <"$WORK/media.tar" \
  || fail "the media archive did not unpack"
dc run --rm --no-deps media-init >/dev/null 2>&1 || fail "media-init did not hand the restored media to the runtime user"
for side in storefront operations media; do
  case "$side" in
    storefront) now="$(content db "$SF_BOOT" storefront)" was="$storefront_before" ;;
    operations) now="$(content operations-db "$OPS_BOOT" operations)" was="$operations_before" ;;
    media) now="$(media)" was="$media_before" ;;
  esac
  [ "$now" = "$was" ] || { diff <(echo "$was") <(echo "$now") || true; fail "the restored $side differs from its backup"; }
done
echo "OK every table and sequence of both databases holds the same content, $(grep -c . <<<"$media_before") media files the same bytes"
release
echo "OK restored and released: the migration jobs found both schemas at head"

echo "== what came back works =="
healthy() { [ "$(served /api/health | cut -d' ' -f1)" = 200 ]; }
until_ok 120 healthy || fail "the restored stand does not answer through nginx"
read -r photo expected <<<"$(grep -m1 '^products/' <<<"$media_before")"
[ "$(served "/media/$photo")" = "200 $expected" ] || fail "nginx does not serve /media/$photo as it was backed up"
echo "OK nginx serves /media/$photo as it was backed up"
from_sources="$(sources)" from_projections="$(projections)"
[ "$from_sources" = "$from_projections" ] || {
  diff <(echo "$from_sources") <(echo "$from_projections") || true
  fail "the restored projections do not stand where their sources stand"
}
echo "OK $(grep -c . <<<"$from_sources") orders and products stand in operations exactly where the storefront has them"
dc exec -T backend python manage.py emit_source_state --snapshot >"$WORK/snapshot.log"
run="$(grep -o 'snapshot run [0-9a-f]*' "$WORK/snapshot.log" | cut -d' ' -f3)"
[ -n "$run" ] || fail "no snapshot run was emitted"
until_ok 180 dc exec -T operations-api python manage.py reconcile --run-id "$run" || {
  dc exec -T operations-api python manage.py reconcile --run-id "$run" | tail -20
  fail "operations does not reconcile the restored storefront"
}
echo "OK a snapshot run $run taken after the restore reconciles without a discrepancy"

echo "REHEARSAL OK"

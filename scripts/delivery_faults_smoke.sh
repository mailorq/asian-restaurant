#!/usr/bin/env bash
# order events and transition commands through broker outages and process crashes, on the production render in an isolated
# compose project. a narrow window is entered at an observed point: a trigger holds the write that follows a publisher
# confirm, or the inbox write that precedes an ack, on an advisory lock, and the script acts only once it sees the process
# waiting there. the verdict is read from both databases, the outboxes, the command and the queues
# without arguments it runs the bounded scenarios ci runs; --all adds a long broker outage and crashes inside a burst
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/disposable_project.sh"

PROJ="ar_faults_$(date +%s)_$$"
claim_project "$PROJ"
WORK="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
fi
ENV_FILE="$WORK/production.env"
HOLDER="${PROJ}_holder"
SF_BOOT=faults_boot
OPS_BOOT=faults_ops_boot
LOCK=4242

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml "$@"; }
cleanup() {
  local status=$?
  echo "== teardown (only $PROJ) =="
  docker rm -f "$HOLDER" >/dev/null 2>&1 || true
  dc --profile provision down -v --remove-orphans >/dev/null 2>&1 || true
  docker image rm "asian-restaurant/storefront:$PROJ" "asian-restaurant/operations:$PROJ" >/dev/null 2>&1 || true
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
  until "$@" >/dev/null 2>&1; do [ "$SECONDS" -lt "$deadline" ] || return 1; sleep 1; done
}
until_true() {  # until_true <seconds> <expression>: evaluated again on every try, unlike the arguments of until_ok
  local deadline=$((SECONDS + $1))
  until eval "$2" >/dev/null 2>&1; do [ "$SECONDS" -lt "$deadline" ] || return 1; sleep 1; done
}
expect() {  # expect <what> <expected> <actual>
  [ "$2" = "$3" ] || fail "$1: expected $2, got $3"
}

write_env() {
  openssl genrsa -out "$WORK/identity.pem" 2048 2>/dev/null
  # read inside the container as 10001
  chmod 644 "$WORK/identity.pem"
  local sf_mq ops_mq bridge_mq commands_mq
  sf_mq="$(rnd)" ops_mq="$(rnd)" bridge_mq="$(rnd)" commands_mq="$(rnd)"
  cat > "$ENV_FILE" <<EOF
SOURCE_REVISION=$PROJ
POSTGRES_DB=storefront
POSTGRES_USER=$SF_BOOT
POSTGRES_PASSWORD=$(rnd)
STOREFRONT_DB_MIGRATOR_PASSWORD=$(rnd)
STOREFRONT_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=$OPS_BOOT
OPERATIONS_DB_PASSWORD=$(rnd)
OPERATIONS_DB_MIGRATOR_PASSWORD=$(rnd)
OPERATIONS_DB_RUNTIME_PASSWORD=$(rnd)
DJANGO_SECRET_KEY=faults-$(rnd)$(rnd)
DJANGO_ALLOWED_HOSTS=faults.invalid,backend
REDIS_URL=redis://redis:6379/1
CART_REDIS_URL=redis://redis:6379/0
RABBITMQ_URL=amqp://storefront_app:$sf_mq@rabbitmq:5672/storefront
IDENTITY_JWT_KID=faults-1
IDENTITY_JWT_KEY_FILE=$WORK/identity.pem
RABBITMQ_ADMIN_USER=faults_admin
RABBITMQ_ADMIN_PASSWORD=$(rnd)
RABBITMQ_ERLANG_COOKIE=$(rnd)
STOREFRONT_MQ_PASSWORD=$sf_mq
OPERATIONS_MQ_PASSWORD=$ops_mq
BRIDGE_MQ_PASSWORD=$bridge_mq
OPERATIONS_COMMANDS_MQ_PASSWORD=$commands_mq
OPERATIONS_SECRET_KEY=faults-$(rnd)$(rnd)
OPERATIONS_ALLOWED_HOSTS=faults.invalid,operations-api
OPERATIONS_RABBITMQ_URL=amqp://operations_consumer:$ops_mq@rabbitmq:5672/operations
OPERATIONS_BRIDGE_CONSUME_URL=amqp://operations_bridge:$bridge_mq@rabbitmq:5672/storefront
OPERATIONS_BRIDGE_PUBLISH_URL=amqp://operations_bridge:$bridge_mq@rabbitmq:5672/operations
OPERATIONS_COMMANDS_RABBITMQ_URL=amqp://operations_commands:$commands_mq@rabbitmq:5672/storefront
EOF
  # the side containers of the script take their broker credentials from files, never from a command line
  echo "URL=amqp://operations_bridge:$bridge_mq@rabbitmq:5672/storefront" > "$WORK/bridge.env"
  echo "URL=amqp://operations_commands:$commands_mq@rabbitmq:5672/storefront" > "$WORK/commands.env"
}

release() {
  echo "== release of the production render =="
  dc build backend operations-api >"$WORK/build.log" 2>&1 || { tail -30 "$WORK/build.log"; fail "the images did not build"; }
  dc up -d rabbitmq >/dev/null 2>&1
  until_ok 180 dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q check_running || fail "the broker never started"
  dc --profile provision run --rm rabbitmq-provision >/dev/null 2>&1 || fail "rabbitmq-provision failed"
  dc up --exit-code-from storefront-migrate storefront-migrate >"$WORK/migrate.log" 2>&1 || { tail -20 "$WORK/migrate.log"; fail "storefront migrations failed"; }
  dc up --exit-code-from operations-migrate operations-migrate >>"$WORK/migrate.log" 2>&1 || { tail -20 "$WORK/migrate.log"; fail "operations migrations failed"; }
  dc up -d relay commands-consumer commands-relay operations-bridge operations-consumer >"$WORK/up.log" 2>&1 || { tail -30 "$WORK/up.log"; fail "the delivery processes did not start"; }
}

# databases, as the bootstrap superuser through the local socket of each container
sf_sql() { dc exec -T db psql -X -q -At -v ON_ERROR_STOP=1 -U "$SF_BOOT" -d storefront "$@"; }
ops_sql() { dc exec -T operations-db psql -X -q -At -v ON_ERROR_STOP=1 -U "$OPS_BOOT" -d operations "$@"; }
be() { dc exec -T "$@" backend python manage.py shell; }
ops() { dc exec -T "$@" operations-api python manage.py shell; }
mq() { dc exec -T -u rabbitmq rabbitmq rabbitmqctl -q "$@"; }
depth() {  # depth <vhost> <queue>
  mq list_queues --no-table-headers -p "$1" name messages | awk -v q="$2" '$1 == q { print $2 }'
}
metric() {  # metric <service> <port> <name> [label fragment...]: the sum of the matching samples
  dc exec -T "$1" python -c "
import sys, time, urllib.request
# a process that has just restarted answers once its metrics server is up
for _ in range(60):
    try:
        text = urllib.request.urlopen('http://127.0.0.1:$2/metrics', timeout=5).read().decode()
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit('no metrics on :$2')
print(int(sum(float(line.rsplit(' ', 1)[1]) for line in text.splitlines()
              if line.startswith(sys.argv[1] + '{') and all(part in line for part in sys.argv[2:]))))
" "${@:3}"
}
restarts() { docker inspect -f '{{.RestartCount}}' "$(dc ps -q "$1")"; }
# compose start and stop act on the dependencies of a service too, these act on its own containers only
stop_only() { local s; for s in "$@"; do docker stop "$(dc ps -aq "$s")" >/dev/null; done; }
start_only() { local s; for s in "$@"; do docker start "$(dc ps -aq "$s")" >/dev/null; done; }
idempotent() { metric operations-consumer 9101 operations_projection_events_total 'outcome="idempotent"'; }
out_of_order() { metric operations-consumer 9101 operations_projection_events_total 'outcome="out_of_order"'; }
replayed() { metric commands-consumer 9102 commands_applied_total 'outcome="replayed"'; }

# the hold: a trigger makes one chosen write wait on an advisory lock that a session of the script keeps
hold_on() {  # hold_on <sql fn> <BEFORE event ON table> <when>
  "$1" -c "CREATE OR REPLACE FUNCTION faults_hold() RETURNS trigger LANGUAGE plpgsql AS \$\$
           BEGIN PERFORM pg_advisory_xact_lock($LOCK); RETURN NEW; END \$\$"
  "$1" -c "CREATE TRIGGER faults_hold BEFORE $2 FOR EACH ROW WHEN ($3) EXECUTE FUNCTION faults_hold()"
}
hold_off() {  # hold_off <sql fn> <table>
  "$1" -c "DROP TRIGGER IF EXISTS faults_hold ON $2"
}
lock() {  # lock <db service> <user> <database>
  dc exec -d "$1" psql -X -q -U "$2" -d "$3" -c "SET application_name = 'faults_holder'" \
    -c "SELECT pg_advisory_lock($LOCK)" -c "SELECT pg_sleep(900)"
}
lock_held() {  # lock_held <sql fn>
  [ "$("$1" -c "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
                WHERE a.application_name = 'faults_holder' AND l.locktype = 'advisory' AND l.granted")" = 1 ]
}
unlock() {  # unlock <sql fn>
  "$1" -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = 'faults_holder'" >/dev/null
}
waiting_pid() {  # waiting_pid <sql fn> <statement prefix>: the backend waiting on the lock with that statement
  "$1" -c "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND wait_event = 'advisory'
           AND query LIKE '$2%'"
}
waiting() { [ -n "$(waiting_pid "$@")" ]; }
end_backend() {  # end_backend <sql fn> <pid>
  "$1" -c "SELECT pg_terminate_backend($2)" >/dev/null
}
# the catch: a redelivery first reads the inbox by its own id, so a session that holds the whole inbox table makes that
# read wait where pg_stat_activity shows which id came back
catch() {  # catch <db service> <user> <database> <table>
  dc exec -d "$1" psql -X -q -U "$2" -d "$3" -c "SET application_name = 'faults_catcher'" -c "BEGIN" \
    -c "LOCK TABLE $4 IN ACCESS EXCLUSIVE MODE" -c "SELECT pg_sleep(900)"
}
catch_queued() {  # catch_queued <sql fn>: the catcher waits for its table behind the held transaction
  [ "$("$1" -c "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'faults_catcher'
                AND wait_event_type = 'Lock' AND query LIKE 'LOCK TABLE%'")" = 1 ]
}
caught() {  # caught <sql fn>: the catcher holds its table, so the transaction it queued behind has ended
  [ "$("$1" -c "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)
                WHERE a.application_name = 'faults_catcher' AND l.locktype = 'relation'
                AND l.mode = 'AccessExclusiveLock' AND l.granted")" = 1 ]
}
read_waiting() {  # read_waiting <sql fn> <table> <id>: a read of the table for that id waits on the catcher
  # django sends a uuid as its hex digits, without the dashes
  [ -n "$("$1" -c "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE 'SELECT%'
                   AND query LIKE '%FROM \"$2\"%' AND query LIKE '%''${3//-/}''::uuid%'")" ]
}
release_catch() {  # release_catch <sql fn>
  "$1" -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = 'faults_catcher'" >/dev/null
}
# the broker connection of a service, by the address it connects from
drop_broker_connection() {  # drop_broker_connection <service> <network>
  local address pid
  address="$(docker inspect -f "{{(index .NetworkSettings.Networks \"${PROJ}_$2\").IPAddress}}" "$(dc ps -q "$1")")"
  pid="$(mq list_connections --no-table-headers pid peer_host | awk -v a="$address" '$2 == a { print $1 }')"
  [ -n "$pid" ] || fail "no broker connection of $1 from $address"
  for p in $pid; do mq close_connection "$p" "faults: connection dropped by the test" >/dev/null; done
}
drop_vhost_connection() {  # drop_vhost_connection <user> <vhost>: the connections of that user to that vhost only
  local pid
  pid="$(mq list_connections --no-table-headers pid user vhost | awk -v u="$1" -v v="$2" '$2 == u && $3 == v { print $1 }')"
  [ -n "$pid" ] || fail "no connection of $1 to $2"
  for p in $pid; do mq close_connection "$p" "faults: connection dropped by the test" >/dev/null; done
}

seed() {
  echo "== seed: a dish, a customer, a superuser and an operator =="
  local out
  out="$(be <<'PY'
import secrets
from decimal import Decimal
from django.contrib.auth import get_user_model
from accounts.roles import StaffRole, set_staff_role
from menu.models import Product
from orders.models import DeliveryAddress

User = get_user_model()
Product.objects.create(code="faults_dish", category="dish", name="Рамен", price=Decimal("50.00"),
                       stock_quantity=1000, version=1)
customer = User.objects.create_user(username="+79990000001", phone="+79990000001", password=secrets.token_hex(16),
                                    first_name="Анна", customer_version=1)
DeliveryAddress.objects.create(user=customer, address="ул. Отказов 1", is_verified=True)
root = User.objects.create_superuser(username="faults_root", password=secrets.token_hex(16))
staff = User.objects.create_user(username="+79990000002", phone="+79990000002", password=secrets.token_hex(16),
                                 customer_version=1)
staff = set_staff_role(actor=root, target=staff, role=StaffRole.OPERATOR)
print("staff", staff.id, staff.authz_version)
PY
)"
  read -r _ ACTOR_ID AUTHZ_VERSION <<<"$(grep '^staff ' <<<"$out" | tr -d '\r')"
  [ -n "${AUTHZ_VERSION:-}" ] || fail "the seed did not report the operator: $out"
  until_ok 60 operator_known || fail "the operator never reached operations"
}
operator_known() {
  [ "$(ops_sql -c "SELECT count(*) FROM operations_employeeauthorization WHERE subject_id = '$ACTOR_ID' AND role_active")" = 1 ]
}

new_order() {  # prints the id of an order created and announced as order.created v1
  be <<'PY' | tr -d '\r' | tail -1
import uuid
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import transaction
from menu.models import Product
from orders.models import DeliveryAddress, Order, OrderItem, OrderStatusHistory
from orders.service import emit_order_state

customer = get_user_model().objects.get(username="+79990000001")
product = Product.objects.get(code="faults_dish")
with transaction.atomic():
    order = Order.objects.create(user=customer, status="created", payment_method="cash", phone=customer.phone,
                                 contact_name="Анна", delivery_address=DeliveryAddress.objects.get(user=customer),
                                 total=Decimal("50.00"), idempotency_key=f"faults-{uuid.uuid4()}",
                                 source_cart_id=f"faults-{uuid.uuid4()}", source_cart_version=1)
    OrderItem.objects.create(order=order, product=product, product_name=product.name, unit_price=Decimal("50.00"),
                             quantity=1, line_total=Decimal("50.00"))
    OrderStatusHistory.objects.create(order=order, from_status="", to_status="created", note="faults")
    emit_order_state(order)
print(order.id)
PY
}
move() {  # move <order> <status>: a status change as the storefront makes it
  be -e ORDER="$1" -e STATUS="$2" <<'PY' >/dev/null
import os
from django.contrib.auth import get_user_model
from django.db import transaction
from orders.models import Order
from orders.service import transition

with transaction.atomic():
    transition(Order.objects.get(pk=int(os.environ["ORDER"])), os.environ["STATUS"],
               changed_by=get_user_model().objects.get(username="faults_root"))
PY
}
command_for() {  # command_for <order> <expected> <target>: prints the command id
  ops -e ACTOR="$ACTOR_ID" -e AUTHZ="$AUTHZ_VERSION" -e ORDER="$1" -e EXPECTED="$2" -e TARGET="$3" <<'PY' | tr -d '\r' | tail -1
import os, uuid
from django.db import connection
from operations.commands import create_transition_command

command, _ = create_transition_command(
    actor_id=int(os.environ["ACTOR"]), actor_authz_version=int(os.environ["AUTHZ"]), order_id=int(os.environ["ORDER"]),
    expected_status=os.environ["EXPECTED"], target_status=os.environ["TARGET"], idempotency_key=str(uuid.uuid4()),
)
print(command.command_id)
# a pool left to the interpreter exit holds the shell for its worker timeouts
connection.close_pool()
PY
}

# facts read back from the stores
projected() {  # projected <order>: "<version> <status>" of the operations projection
  ops_sql -c "SELECT aggregate_version || ' ' || status FROM operations_operationorder WHERE source_order_id = $1"
}
projected_is() { [ "$(projected "$1")" = "$2" ]; }
history() { sf_sql -c "SELECT count(*) FROM orders_orderstatushistory WHERE order_id = $1"; }
sf_status() { sf_sql -c "SELECT status FROM orders_order WHERE id = $1"; }
outbox_row() {  # outbox_row <order> <version>: "<status> <lease held>"
  sf_sql -c "SELECT status || ' ' || (lease_token IS NOT NULL) FROM orders_orderoutbox
             WHERE aggregate_type = 'order' AND aggregate_id = '$1' AND aggregate_version = $2 AND NOT snapshot"
}
event_of() { sf_sql -c "SELECT event_id FROM orders_orderoutbox WHERE aggregate_type = 'order' AND aggregate_id = '$1'
                        AND aggregate_version = $2 AND NOT snapshot"; }
inbox_count() { ops_sql -c "SELECT count(*) FROM operations_inboxevent WHERE event_id = '$1'"; }
command_status() { ops_sql -c "SELECT status FROM operations_operationcommand WHERE command_id = '$1'"; }
command_status_is() { [ "$(command_status "$1")" = "$2" ]; }
command_row() {  # command_row <command>: "<outbox status> <attempts>"
  ops_sql -c "SELECT o.status || ' ' || o.attempts FROM operations_operationsoutbox o
              JOIN operations_operationcommand c ON c.id = o.command_id WHERE c.command_id = '$1'"
}
applied_inbox() { sf_sql -c "SELECT count(*) FROM orders_commandinbox WHERE command_id = '$1'"; }
outcomes() { sf_sql -c "SELECT count(*) FROM orders_orderoutbox WHERE event_type LIKE 'orders.transition.%'
                        AND payload->>'command_id' = '$1'"; }

scenario_relay_confirm_unrecorded() {
  echo "== an order event the broker confirmed but the relay never recorded =="
  local order event pid restarted
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  hold_on sf_sql "UPDATE ON orders_orderoutbox" \
    "OLD.aggregate_id = '$order' AND OLD.aggregate_version = 2 AND OLD.status = 'pending' AND NEW.status = 'published'"
  lock db "$SF_BOOT" storefront
  until_ok 30 lock_held sf_sql || fail "the holder never took the lock"
  move "$order" confirmed
  until_ok 30 waiting sf_sql 'UPDATE "orders_orderoutbox"' || fail "the relay never reached the write after the confirm"
  pid="$(waiting_pid sf_sql 'UPDATE "orders_orderoutbox"')"
  # the confirm is real: the event already crossed the bridge while the relay still holds the row pending
  until_ok 60 projected_is "$order" "2 confirmed" || fail "the confirmed event never reached the projection"
  expect "the outbox row while the relay waits" "pending true" "$(outbox_row "$order" 2)"
  restarted="$(restarts relay)"
  # the write after the confirm fails under the relay, and the relay dies on it
  end_backend sf_sql "$pid"
  unlock sf_sql
  hold_off sf_sql orders_orderoutbox
  until_true 60 '[ "$(restarts relay)" -gt "$restarted" ]' || fail "the relay did not die and come back"
  expect "the outbox row after the relay died" "pending true" "$(outbox_row "$order" 2)"
  event="$(event_of "$order" 2)"
  local before
  before="$(idempotent)"
  # the lease the dead relay took runs out; the clock is moved instead of waited for
  sf_sql -c "UPDATE orders_orderoutbox SET locked_until = now() - interval '1 second' WHERE event_id = '$event'" >/dev/null
  until_true 120 '[ "$(outbox_row "$order" 2)" = "published false" ]' || fail "the restarted relay never republished"
  until_true 60 '[ "$(idempotent)" -gt "$before" ]' \
    || fail "the second delivery of $event never reached the consumer"
  expect "inbox rows of the event delivered twice" 1 "$(inbox_count "$event")"
  expect "the projection after the second delivery" "2 confirmed" "$(projected "$order")"
  expect "the status history" 2 "$(history "$order")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  ok "a confirmed event the relay did not record is published again and applied once"
}

# a side container that takes the head of the bridge queue and keeps it unacknowledged until it is told to let go
hold_bridge_head() {
  docker run -d --name "$HOLDER" --pull never --network "${PROJ}_broker_operations" --env-file "$WORK/bridge.env" \
    --entrypoint python "asian-restaurant/operations:$PROJ" -c '
import os, pika
conn = pika.BlockingConnection(pika.URLParameters(os.environ["URL"]))
channel = conn.channel()
method, props, _ = channel.basic_get("operations.bridge.storefront", auto_ack=False)
print("holding", props.type if method else "nothing", flush=True)
while method and not os.path.exists("/tmp/release"):
    conn.process_data_events(time_limit=1)
if method:
    channel.basic_nack(method.delivery_tag, requeue=True)
conn.close()
print("released", flush=True)
' >/dev/null
}
holder_says() { docker logs "$HOLDER" 2>&1 | grep -q "^$1"; }
let_go() {
  docker exec "$HOLDER" touch /tmp/release
  until_ok 30 holder_says released || fail "the holder never gave the message back"
  docker rm -f "$HOLDER" >/dev/null
}
# the event of a new order is held at the bridge while its next event passes it
reorder() {  # reorder <order>: leaves order.created held and order.status_changed delivered behind it
  stop_only operations-bridge
  until_true 30 '[ "$(depth storefront operations.bridge.storefront)" = 2 ]' \
    || fail "the two events of order $1 never reached the bridge queue"
  hold_bridge_head
  until_ok 30 holder_says "holding order.created" || fail "the holder did not take order.created: $(docker logs "$HOLDER" 2>&1)"
  start_only operations-bridge
}

scenario_reorder_within_budget() {
  echo "== a status change that overtakes the creation of its order by less than the retry budget =="
  local order before
  before="$(out_of_order)"
  stop_only operations-bridge
  order="$(new_order)"
  move "$order" confirmed
  reorder "$order"
  until_true 60 '[ "$(out_of_order)" -gt "$before" ]' \
    || fail "the status change never arrived ahead of the creation"
  expect "the projection while the creation is held" "" "$(projected "$order")"
  let_go
  until_ok 60 projected_is "$order" "2 confirmed" || fail "order $order did not converge: $(projected "$order")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  expect "inbox rows of the creation" 1 "$(inbox_count "$(event_of "$order" 1)")"
  expect "inbox rows of the status change" 1 "$(inbox_count "$(event_of "$order" 2)")"
  ok "an early status change waits in the retry queue and is applied once after the creation"
}

scenario_reorder_past_budget() {
  echo "== a status change that overtakes the creation by more than the retry budget, then an operator replay =="
  local order event
  stop_only operations-bridge
  order="$(new_order)"
  move "$order" confirmed
  event="$(event_of "$order" 2)"
  reorder "$order"
  until_true 120 '[ "$(depth operations operations.projection.dlq)" = 1 ]' \
    || fail "the status change never exhausted its retries"
  let_go
  until_ok 60 projected_is "$order" "1 created" || fail "the creation never arrived: $(projected "$order")"
  expect "inbox rows of the dead lettered event" 0 "$(inbox_count "$event")"
  dc exec -T operations-consumer python manage.py dlq --list >/dev/null
  expect "the dead letters after a listing" 1 "$(depth operations operations.projection.dlq)"
  dc exec -T operations-consumer python manage.py dlq --replay 1 --operator faults >/dev/null
  until_ok 60 projected_is "$order" "2 confirmed" || fail "the replayed status change was not applied: $(projected "$order")"
  expect "the dead letters after the replay" 0 "$(depth operations operations.projection.dlq)"
  expect "inbox rows after the replay" 1 "$(inbox_count "$event")"
  ok "a status change dead lettered behind its creation is applied once by the operator replay"
}

scenario_consumer_commit_before_ack() {
  echo "== the consumer commits an event and loses its broker connection before the ack =="
  local order event
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  hold_on ops_sql "INSERT ON operations_inboxevent" \
    "NEW.event_type = 'orders.order.status_changed.v1' AND NEW.aggregate_version = 2"
  lock operations-db "$OPS_BOOT" operations
  until_ok 30 lock_held ops_sql || fail "the holder never took the lock"
  move "$order" confirmed
  until_ok 60 waiting ops_sql 'INSERT INTO "operations_inboxevent"' || fail "the consumer never reached the inbox write"
  event="$(event_of "$order" 2)"
  drop_broker_connection operations-consumer broker_operations
  catch operations-db "$OPS_BOOT" operations operations_inboxevent
  until_ok 30 catch_queued ops_sql || fail "the catcher never queued for the inbox table"
  # the transaction is let through: it commits after the connection that would carry the ack is gone
  unlock ops_sql
  until_ok 60 caught ops_sql || fail "the held transaction never finished"
  expect "the projection after the commit" "2 confirmed" "$(projected "$order")"
  until_ok 120 read_waiting ops_sql operations_inboxevent "$event" || fail "event $event was never delivered again"
  release_catch ops_sql
  hold_off ops_sql operations_inboxevent
  until_true 60 '[ "$(depth operations operations.projection)" = 0 ]' || fail "the redelivered event was never acknowledged"
  expect "inbox rows of the event" 1 "$(inbox_count "$event")"
  expect "the projection" "2 confirmed" "$(projected "$order")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  ok "an event committed before a lost ack comes back by its own event id and its effect is applied once"
}

scenario_command_confirm_unrecorded_settled() {
  echo "== a command the broker confirmed but the relay never recorded, settled by its outcome =="
  local order command pid restarted
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  hold_on ops_sql "UPDATE ON operations_operationsoutbox" \
    "OLD.aggregate_id = '$order' AND OLD.status = 'pending' AND NEW.status = 'published'"
  lock operations-db "$OPS_BOOT" operations
  until_ok 30 lock_held ops_sql || fail "the holder never took the lock"
  command="$(command_for "$order" created confirmed)"
  until_ok 60 waiting ops_sql 'UPDATE "operations_operationsoutbox"' || fail "the relay never reached the write after the confirm"
  pid="$(waiting_pid ops_sql 'UPDATE "operations_operationsoutbox"')"
  until_true 60 '[ "$(sf_status "$order")" = confirmed ]' || fail "the storefront never applied the confirmed command"
  restarted="$(restarts commands-relay)"
  end_backend ops_sql "$pid"
  unlock ops_sql
  hold_off ops_sql operations_operationsoutbox
  until_true 60 '[ "$(restarts commands-relay)" -gt "$restarted" ]' || fail "the command relay did not die and come back"
  until_ok 120 command_status_is "$command" succeeded || fail "the outcome never closed the command: $(command_status "$command")"
  expect "the command outbox row" settled "$(command_row "$command" | cut -d' ' -f1)"
  expect "storefront inbox rows" 1 "$(applied_inbox "$command")"
  expect "outcome events" 1 "$(outcomes "$command")"
  expect "the status history" 2 "$(history "$order")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  expect "the command dead letters" 0 "$(depth storefront commands.orders.dlq)"
  ok "an outcome settles a command whose confirm was lost, and nothing publishes it again"
}

scenario_command_confirm_unrecorded_republished() {
  echo "== a command the broker confirmed but the relay never recorded, published again before its outcome =="
  local order command pid before restarted
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  # the outcome stays in the storefront outbox, so the second publish comes first
  stop_only relay
  hold_on ops_sql "UPDATE ON operations_operationsoutbox" \
    "OLD.aggregate_id = '$order' AND OLD.status = 'pending' AND NEW.status = 'published'"
  lock operations-db "$OPS_BOOT" operations
  until_ok 30 lock_held ops_sql || fail "the holder never took the lock"
  command="$(command_for "$order" created confirmed)"
  until_ok 60 waiting ops_sql 'UPDATE "operations_operationsoutbox"' || fail "the relay never reached the write after the confirm"
  pid="$(waiting_pid ops_sql 'UPDATE "operations_operationsoutbox"')"
  until_true 60 '[ "$(sf_status "$order")" = confirmed ]' || fail "the storefront never applied the confirmed command"
  restarted="$(restarts commands-relay)"
  end_backend ops_sql "$pid"
  unlock ops_sql
  hold_off ops_sql operations_operationsoutbox
  until_true 60 '[ "$(restarts commands-relay)" -gt "$restarted" ]' || fail "the command relay did not die and come back"
  expect "the command after the relay died" pending "$(command_status "$command")"
  before="$(replayed)"
  ops_sql -c "UPDATE operations_operationsoutbox SET locked_until = now() - interval '1 second'
              WHERE command_id = (SELECT id FROM operations_operationcommand WHERE command_id = '$command')" >/dev/null
  until_true 120 '[ "$(command_row "$command" | cut -d" " -f1)" = published ]' || fail "the command was never published again"
  until_true 60 '[ "$(replayed)" -gt "$before" ]' \
    || fail "the second delivery never reached the storefront"
  expect "storefront inbox rows" 1 "$(applied_inbox "$command")"
  expect "outcome events" 1 "$(outcomes "$command")"
  expect "the status history" 2 "$(history "$order")"
  start_only relay
  until_ok 120 command_status_is "$command" succeeded || fail "the outcome never closed the command: $(command_status "$command")"
  expect "outcome events after the relay came back" 1 "$(outcomes "$command")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  expect "the command dead letters" 0 "$(depth storefront commands.orders.dlq)"
  ok "a command published twice is applied once and replays its recorded outcome"
}

scenario_command_consumer_commit_before_ack() {
  echo "== the storefront commits a command and loses its broker connection before the ack =="
  local order command
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  hold_on sf_sql "INSERT ON orders_commandinbox" "true"
  lock db "$SF_BOOT" storefront
  until_ok 30 lock_held sf_sql || fail "the holder never took the lock"
  command="$(command_for "$order" created confirmed)"
  until_ok 60 waiting sf_sql 'INSERT INTO "orders_commandinbox"' || fail "the command consumer never reached the inbox write"
  drop_broker_connection commands-consumer broker_storefront
  catch db "$SF_BOOT" storefront orders_commandinbox
  until_ok 30 catch_queued sf_sql || fail "the catcher never queued for the inbox table"
  unlock sf_sql
  until_ok 60 caught sf_sql || fail "the held transaction never finished"
  expect "the order after the commit" confirmed "$(sf_status "$order")"
  until_ok 120 read_waiting sf_sql orders_commandinbox "$command" || fail "command $command was never delivered again"
  release_catch sf_sql
  hold_off sf_sql orders_commandinbox
  until_true 60 '[ "$(depth storefront commands.orders)" = 0 ]' || fail "the redelivered command was never acknowledged"
  until_ok 120 command_status_is "$command" succeeded || fail "the command never closed: $(command_status "$command")"
  expect "storefront inbox rows" 1 "$(applied_inbox "$command")"
  expect "outcome events" 1 "$(outcomes "$command")"
  expect "the status history" 2 "$(history "$order")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  expect "the command dead letters" 0 "$(depth storefront commands.orders.dlq)"
  ok "a command committed before a lost ack comes back by its own command id and replays its recorded outcome"
}

expire() {  # expire <command>: its deadline passed, the clock is moved instead of waited for
  ops_sql -c "UPDATE operations_operationcommand SET deadline_at = now() - interval '1 second'
              WHERE command_id = '$1'" >/dev/null
}
result_code() { ops_sql -c "SELECT result_code FROM operations_operationcommand WHERE command_id = '$1'"; }

scenario_command_expired_unsent() {
  echo "== a command whose deadline passed before any publish attempt =="
  local order command
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  stop_only commands-relay
  command="$(command_for "$order" created confirmed)"
  expire "$command"
  start_only commands-relay
  until_ok 60 command_status_is "$command" rejected || fail "the expired command stayed $(command_status "$command")"
  expect "the reason" command_expired "$(result_code "$command")"
  expect "the command outbox row" suppressed "$(command_row "$command" | cut -d' ' -f1)"
  expect "storefront inbox rows" 0 "$(applied_inbox "$command")"
  expect "the order" created "$(sf_status "$order")"
  ok "a command never sent is closed locally as expired and never reaches the storefront"
}

scenario_command_timed_out_then_outcome() {
  echo "== a dispatched command passes its deadline, then its outcome arrives late =="
  local order command
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  stop_only commands-consumer
  command="$(command_for "$order" created confirmed)"
  until_ok 60 command_status_is "$command" dispatched || fail "the command was never dispatched"
  expire "$command"
  until_ok 60 command_status_is "$command" timed_out || fail "the sweep never timed the command out"
  start_only commands-consumer
  until_ok 120 command_status_is "$command" succeeded || fail "the late outcome never closed the command"
  expect "the order" confirmed "$(sf_status "$order")"
  expect "storefront inbox rows" 1 "$(applied_inbox "$command")"
  expect "outcome events" 1 "$(outcomes "$command")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  expect "the command dead letters" 0 "$(depth storefront commands.orders.dlq)"
  ok "a timed out command is closed by its late outcome"
}

scenario_command_dispatch_failed() {
  echo "== a command the relay cannot hand to the broker, closed by the operator =="
  local order command container
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  stop_only commands-relay
  container="$(dc ps -aq commands-relay)"
  docker network disconnect "${PROJ}_broker_operations" "$container"
  command="$(command_for "$order" created confirmed)"
  start_only commands-relay
  # each failed attempt is followed at once instead of after its backoff
  until_ok 120 failed_after_retries "$command" || fail "the command never ran out of attempts: $(command_row "$command")"
  expect "the command" dispatch_failed "$(command_status "$command")"
  docker network connect "${PROJ}_broker_operations" "$container"
  expect "storefront inbox rows" 0 "$(applied_inbox "$command")"
  dc exec -T operations-api python manage.py resolve_command "$command" --outcome rejected --operator faults \
    --reason "the storefront holds no inbox row for it" >/dev/null
  expect "the command after the verdict" rejected "$(command_status "$command")"
  expect "the order" created "$(sf_status "$order")"
  ok "a command that never reached the broker waits as dispatch_failed and is closed by the operator"
}
failed_after_retries() {
  ops_sql -c "UPDATE operations_operationsoutbox SET next_attempt_at = now() WHERE status = 'pending'
              AND command_id = (SELECT id FROM operations_operationcommand WHERE command_id = '$1')" >/dev/null
  [ "$(command_status "$1")" = dispatch_failed ]
}

scenario_command_dead_letter() {
  echo "== a message on the command queue that is no command =="
  local before
  before="$(depth storefront commands.orders.dlq)"
  docker run --rm --pull never --network "${PROJ}_broker_operations" --env-file "$WORK/commands.env" \
    --entrypoint python "asian-restaurant/operations:$PROJ" -c '
import os, pika
conn = pika.BlockingConnection(pika.URLParameters(os.environ["URL"]))
channel = conn.channel()
channel.confirm_delivery()
channel.basic_publish("commands", "orders.transition.requested", b"not a command", mandatory=True,
                      properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"))
conn.close()
' || fail "the stray message was not accepted by the broker"
  until_true 60 '[ "$(depth storefront commands.orders.dlq)" = "$((before + 1))" ]' || fail "the stray message never reached the dead letters"
  dc exec -T commands-consumer python manage.py dlq commands.orders.dlq --list >/dev/null
  expect "the dead letters after a listing" $((before + 1)) "$(depth storefront commands.orders.dlq)"
  dc exec -T commands-consumer python manage.py dlq commands.orders.dlq --drop 1 --yes --operator faults \
    --reason "not a command" >/dev/null
  expect "the dead letters after the drop" "$before" "$(depth storefront commands.orders.dlq)"
  ok "a message that is no command is dead lettered, listed without being consumed and dropped by the operator"
}

scenario_planned_stop() {
  echo "== a planned stop of every delivery process, as a release stops them =="
  local service container started took code order command
  for service in relay operations-bridge operations-consumer commands-relay commands-consumer; do
    container="$(dc ps -q "$service")"
    started=$SECONDS
    docker stop -t 30 "$container" >/dev/null
    took=$((SECONDS - started))
    code="$(docker inspect -f '{{.State.ExitCode}}' "$container")"
    docker start "$container" >/dev/null
    # 137 is the kill that follows a sigterm the process never received
    [ "$code" != 137 ] || fail "$service ignored sigterm and was killed after $took seconds"
    [ "$took" -lt 10 ] || fail "$service took $took seconds to stop"
  done
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected after the stops"
  command="$(command_for "$order" created confirmed)"
  until_ok 120 command_status_is "$command" succeeded || fail "the command after the stops ended $(command_status "$command")"
  ok "each delivery process stops on sigterm at once and delivery resumes after the start"
}

scenario_bridge_publish_lost() {
  echo "== the bridge loses its connection to operations right before it publishes =="
  local order event since
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  drop_vhost_connection operations_bridge operations
  since="$(date +%s)"
  move "$order" confirmed
  event="$(event_of "$order" 2)"
  until_ok 60 projected_is "$order" "2 confirmed" || fail "the event never got past the bridge: $(projected "$order")"
  docker logs --since "$since" "$(dc ps -q operations-bridge)" 2>&1 | grep -q "bridge publish failed" \
    || fail "the bridge never failed the publish, the retry queue was not exercised"
  expect "inbox rows of the event" 1 "$(inbox_count "$event")"
  expect "the bridge dead letters" 0 "$(depth storefront operations.bridge.storefront.dlq)"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  ok "an event the bridge failed to publish goes through its retry queue and its effect is applied once"
}

scenario_bridge_publish_refused() {
  echo "== the bridge refused by operations past its retry budget, then an operator replay =="
  local order event
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  # a channel keeps the rights it was checked with, so the connection goes with the right to write
  mq set_permissions -p operations operations_bridge '^operations\.events$' '^$' '^$'
  drop_vhost_connection operations_bridge operations
  move "$order" confirmed
  event="$(event_of "$order" 2)"
  until_true 120 '[ "$(depth storefront operations.bridge.storefront.dlq)" = 1 ]' \
    || fail "the refused event never reached the bridge dead letters"
  expect "the projection while the event is dead lettered" "1 created" "$(projected "$order")"
  dc --profile provision run --rm rabbitmq-provision >/dev/null 2>&1 || fail "rabbitmq-provision did not restore the rights"
  dc exec -T operations-bridge python manage.py dlq --queue bridge --list >/dev/null
  expect "the bridge dead letters after a listing" 1 "$(depth storefront operations.bridge.storefront.dlq)"
  dc exec -T operations-bridge python manage.py dlq --queue bridge --replay 1 --operator faults >/dev/null
  until_ok 60 projected_is "$order" "2 confirmed" || fail "the replayed event was not applied: $(projected "$order")"
  expect "the bridge dead letters after the replay" 0 "$(depth storefront operations.bridge.storefront.dlq)"
  expect "inbox rows of the event" 1 "$(inbox_count "$event")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  ok "an event the bridge could not publish within its budget waits in its dead letters and the operator replay applies it once"
}

# long: the deadline of a command is five minutes after it was created and travels in its envelope, so it is waited for
scenario_command_expired_in_transit() {
  echo "== a command that waits in the broker past its deadline =="
  local order command
  order="$(new_order)"
  until_ok 60 projected_is "$order" "1 created" || fail "order $order was never projected"
  stop_only commands-consumer
  command="$(command_for "$order" created confirmed)"
  until_ok 60 command_status_is "$command" dispatched || fail "the command was never dispatched"
  until_ok 420 command_status_is "$command" timed_out || fail "the command never timed out: $(command_status "$command")"
  start_only commands-consumer
  until_ok 120 command_status_is "$command" rejected || fail "the late command ended $(command_status "$command")"
  expect "the reason" command_expired "$(result_code "$command")"
  expect "the order" created "$(sf_status "$order")"
  expect "storefront inbox rows" 1 "$(applied_inbox "$command")"
  expect "outcome events" 1 "$(outcomes "$command")"
  ok "a command delivered after its deadline is refused by the storefront and closes as rejected"
}

# long: the broker away for longer than the relay backoff reaches, while orders move and a command waits
scenario_broker_outage() {
  echo "== the broker away for $OUTAGE seconds while orders move and a command waits =="
  local a b command
  a="$(new_order)" b="$(new_order)"
  until_ok 60 projected_is "$b" "1 created" || fail "the orders were never projected"
  stop_only rabbitmq
  move "$a" confirmed
  move "$b" confirmed
  move "$b" preparing
  command="$(command_for "$a" confirmed preparing)"
  sleep "$OUTAGE"
  expect "the order events while the broker is away" 0 \
    "$(sf_sql -c "SELECT count(*) FROM orders_orderoutbox WHERE aggregate_id IN ('$a', '$b') AND status = 'published' AND aggregate_version > 1")"
  start_only rabbitmq
  until_ok 180 dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q check_running || fail "the broker did not come back"
  local back=$SECONDS
  # a row whose attempts grew in the outage waits out its backoff, which reaches 300 seconds
  until_ok 420 projected_is "$b" "3 preparing" || fail "order $b did not converge: $(projected "$b")"
  until_ok 60 projected_is "$a" "2 confirmed" || fail "order $a did not converge: $(projected "$a")"
  echo "   the order events converged $((SECONDS - back)) seconds after the broker came back"
  for order in "$a" "$b"; do
    for version in $(sf_sql -c "SELECT aggregate_version FROM orders_orderoutbox WHERE aggregate_id = '$order' AND NOT snapshot AND event_type LIKE 'order.%'"); do
      expect "inbox rows of order $order v$version" 1 "$(inbox_count "$(event_of "$order" "$version")")"
    done
  done
  # five attempts spread over far less than the outage: the command waits for the operator
  until_ok 60 command_status_is "$command" dispatch_failed || fail "the command ended $(command_status "$command")"
  expect "storefront inbox rows" 0 "$(applied_inbox "$command")"
  for service in relay commands-consumer commands-relay operations-bridge operations-consumer; do
    expect "$service running" true "$(docker inspect -f '{{.State.Running}}' "$(dc ps -q "$service")")"
  done
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  ok "after the outage the effect of every order event is applied once and the command waits as dispatch_failed"
}

# long: the bridge and the consumer killed in the middle of a burst; docker kill alone would count as a manual stop
# and keep the restart policy from bringing them back, so each is killed and started again in one restart
scenario_crash_in_burst() {
  echo "== the bridge and the consumer killed in the middle of a burst of $BURST orders =="
  local orders total killed
  # the burst queues up behind a stopped bridge, so each kill lands while its queue drains
  stop_only operations-bridge operations-consumer
  orders="$(be -e COUNT="$BURST" <<'PY' | tr -d '\r' | tail -1
import os, uuid
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import transaction
from menu.models import Product
from orders.models import DeliveryAddress, Order, OrderItem, OrderStatusHistory
from orders.service import emit_order_state, transition

customer = get_user_model().objects.get(username="+79990000001")
root = get_user_model().objects.get(username="faults_root")
product = Product.objects.get(code="faults_dish")
ids = []
for _ in range(int(os.environ["COUNT"])):
    with transaction.atomic():
        order = Order.objects.create(user=customer, status="created", payment_method="cash", phone=customer.phone,
                                     contact_name="Анна", delivery_address=DeliveryAddress.objects.get(user=customer),
                                     total=Decimal("50.00"), idempotency_key=f"faults-{uuid.uuid4()}",
                                     source_cart_id=f"faults-{uuid.uuid4()}", source_cart_version=1)
        OrderItem.objects.create(order=order, product=product, product_name=product.name,
                                 unit_price=Decimal("50.00"), quantity=1, line_total=Decimal("50.00"))
        OrderStatusHistory.objects.create(order=order, from_status="", to_status="created", note="faults")
        emit_order_state(order)
    for status in ("confirmed", "preparing", "delivering"):
        with transaction.atomic():
            transition(Order.objects.get(pk=order.pk), status, changed_by=root)
    ids.append(str(order.id))
print(",".join(ids))
PY
)"
  total="$(tr ',' '\n' <<<"$orders" | grep -c .)"
  until_true 120 '[ "$(depth storefront operations.bridge.storefront)" = "$((total * 4))" ]' \
    || fail "the burst never queued up at the bridge: $(depth storefront operations.bridge.storefront)"
  start_only operations-bridge
  killed="$(kill_when_draining storefront operations.bridge.storefront $((total * 4)) operations-bridge)"
  echo "   the bridge was killed with $killed of $((total * 4)) events still queued"
  until_true 120 '[ "$(depth storefront operations.bridge.storefront)" = 0 ]' || fail "the bridge never came back to drain"
  # the consumer outruns any poll of the broker, so it is held inside the transaction of an event from the middle
  local events middle pid
  events="$(sf_sql -c "SELECT string_agg(quote_literal(event_id::text), ',') FROM orders_orderoutbox
                       WHERE aggregate_id IN ($(sed "s/[0-9][0-9]*/'&'/g" <<<"$orders")) AND NOT snapshot")"
  middle="$(tr ',' '\n' <<<"$orders" | sed -n "$((total / 2))p")"
  hold_on ops_sql "INSERT ON operations_inboxevent" "NEW.event_id = '$(event_of "$middle" 2)'"
  lock operations-db "$OPS_BOOT" operations
  until_ok 30 lock_held ops_sql || fail "the holder never took the lock"
  start_only operations-consumer
  until_ok 120 waiting ops_sql 'INSERT INTO "operations_inboxevent"' || fail "the consumer never reached the middle of the burst"
  pid="$(waiting_pid ops_sql 'INSERT INTO "operations_inboxevent"')"
  echo "   the consumer was killed inside a transaction, $(burst_inbox "$events") of $((total * 4)) events applied"
  docker restart -t 0 "$(dc ps -q operations-consumer)" >/dev/null
  end_backend ops_sql "$pid"
  unlock ops_sql
  hold_off ops_sql operations_inboxevent
  until_true 240 '[ "$(burst_done "$orders")" = "$total" ]' || fail "the burst did not converge: $(burst_done "$orders") of $total"
  expect "inbox rows of the burst" $((total * 4)) "$(burst_inbox "$events")"
  expect "the projection dead letters" 0 "$(depth operations operations.projection.dlq)"
  ok "a burst survives the bridge and the consumer killed under it: every event applied once"
}
# the event list goes on stdin: a thousand ids exceed the length of a windows command line
burst_inbox() { ops_sql <<<"SELECT count(*) FROM operations_inboxevent WHERE event_id::text IN ($1)"; }
burst_done() {
  ops_sql -c "SELECT count(*) FROM operations_operationorder WHERE source_order_id IN ($1) AND aggregate_version = 4"
}
kill_when_draining() {  # kill_when_draining <vhost> <queue> <full depth> <service>: prints the depth it was killed at
  local now
  for _ in $(seq 1 600); do
    now="$(depth "$1" "$2")"
    if [ -n "$now" ] && [ "$now" -lt "$3" ]; then
      [ "$now" -gt 0 ] || fail "$4 drained $2 before it could be killed"
      docker restart -t 0 "$(dc ps -q "$4")" >/dev/null
      echo "$now"
      return
    fi
    sleep 0.2
  done
  fail "$4 never started on $2"
}

main() {
  local long=0
  case "${1:-}" in
    "") ;;
    --all) long=1 ;;
    *) fail "usage: delivery_faults_smoke.sh [--all]" ;;
  esac
  OUTAGE=150 BURST=250
  write_env
  release
  seed
  scenario_relay_confirm_unrecorded
  scenario_reorder_within_budget
  scenario_reorder_past_budget
  scenario_consumer_commit_before_ack
  scenario_command_confirm_unrecorded_settled
  scenario_command_confirm_unrecorded_republished
  scenario_command_consumer_commit_before_ack
  scenario_command_expired_unsent
  scenario_command_timed_out_then_outcome
  scenario_command_dispatch_failed
  scenario_command_dead_letter
  scenario_bridge_publish_lost
  scenario_bridge_publish_refused
  scenario_planned_stop
  if [ "$long" = 1 ]; then
    scenario_command_expired_in_transit
    scenario_broker_outage
    scenario_crash_in_burst
  fi
  echo "FAULTS OK"
}
main "$@"

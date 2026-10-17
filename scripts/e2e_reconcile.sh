#!/usr/bin/env bash
# Real end-to-end reconciliation in an ISOLATED Compose project with its own volumes:
#   source models -> emit_source_state -> publish_outbox -> bridge -> consumer -> reconcile
# Uses only tracked config (.env.e2e + compose.e2e.yaml); no gitignored override or local
# .env. Never touches the shared dev/staging project. No manual pika publishing.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/disposable_project.sh"

if [ "${E2E_ALLOW_DESTRUCTIVE:-0}" != "1" ]; then
  echo "refusing to run: this test creates and destroys an isolated stack." >&2
  echo "re-run with E2E_ALLOW_DESTRUCTIVE=1" >&2
  exit 2
fi

# unique project per invocation/CI job so parallel runs never share volumes
PROJ="ar_e2e_$(date +%s)_$$"
claim_project "$PROJ"
CO=(-p "$PROJ" --env-file .env.e2e -f compose.yaml -f compose.e2e.yaml)
dc() { docker compose "${CO[@]}" "$@"; }
be() { dc exec -T backend "$@"; }
ops() { dc exec -T operations-api "$@"; }
bridge_ready() { dc logs operations-bridge 2>&1 | grep -q 'storefront bridge:'; }
card_projected() { ops python manage.py shell -c "import sys; from operations.models import OperationOrder; sys.exit(0 if OperationOrder.objects.filter(payment_method='card').exists() else 1)"; }
order_confirmed() { be python manage.py shell -c "import sys; from accounts.roles import StaffRole, set_staff_role
from orders.models import Order; sys.exit(0 if Order.objects.get(idempotency_key='e2e-key').status == 'confirmed' else 1)"; }
command_succeeded() { ops python manage.py shell -c "import sys; from operations.models import OperationCommand; sys.exit(0 if OperationCommand.objects.filter(idempotency_key='e2e-transition', status='succeeded').exists() else 1)"; }

cleanup() {
  local status=$?
  echo "== teardown (only $PROJ) =="
  dc down -v --remove-orphans --rmi local >/dev/null 2>&1 || true
  if ! teardown_clean "$PROJ"; then [ "$status" -ne 0 ] || status=1; fi
  exit "$status"
}
trap cleanup EXIT

wait_for() {  # wait_for <label> <cmd...>
  local label="$1"; shift
  for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && { echo "ready: $label"; return 0; }; sleep 2; done
  echo "timeout waiting for: $label" >&2; return 1
}

echo "== build + bring up isolated stack (project $PROJ) =="
dc up -d --build db redis rabbitmq operations-db operations-api operations-consumer operations-bridge backend >/dev/null
# probing as root would create a root-owned .erlang.cookie in the data dir and kill the booting server, which runs as rabbitmq
wait_for rabbitmq dc exec -T -u rabbitmq rabbitmq rabbitmq-diagnostics -q ping
wait_for backend-migrated be python manage.py migrate --check
wait_for operations-migrated ops python manage.py migrate --check

echo "== declare storefront 'orders' exchange, then rebind bridge =="
be python manage.py shell <<'PY'
from orders import messaging
messaging.declare_topology(messaging.connect().channel())
print("orders topology declared")
PY
dc restart operations-bridge >/dev/null
wait_for bridge-bound bridge_ready

echo "== seed source models incl. a CARD-payment order =="
be python manage.py shell <<'PY'
from decimal import Decimal
from django.contrib.auth import get_user_model
from menu.models import Product
from orders.models import DeliveryAddress, Order, OrderItem, OrderStatusHistory

User = get_user_model()
p = Product.objects.create(code="e2e_dish", category="dish", name="E2E Рамен",
                           price=Decimal("50.00"), stock_quantity=10, version=1)
u = User.objects.create_user(username="+79995550000", phone="+79995550000",
                             password="Pass!2345", first_name="Иван", customer_version=1)
addr = DeliveryAddress.objects.create(user=u, address="ул. E2E 1", is_verified=True)
o = Order.objects.create(user=u, status="created", payment_method="card", phone=u.phone,
                         contact_name="Иван", delivery_address=addr, total=Decimal("100.00"),
                         idempotency_key="e2e-key", source_cart_id="e2e", source_cart_version=1)
OrderItem.objects.create(order=o, product=p, product_name=p.name,
                         unit_price=Decimal("50.00"), quantity=2, line_total=Decimal("100.00"))
OrderStatusHistory.objects.create(order=o, from_status="", to_status="created", note="seed")
print(f"seeded product={p.code} user={u.id} order={o.id} payment=card")
PY

echo "== emit live state + a snapshot run, then relay through the outbox =="
be python manage.py emit_source_state
be python manage.py emit_source_state --snapshot
for _ in 1 2 3 4 5; do be python manage.py publish_outbox; done

echo "== assert projected payment method is 'card' BEFORE reconciling =="
wait_for card-projected card_projected

echo "== transition command: operations -> relay -> storefront -> outcome -> operations =="
# started only now: the command path is exercised after the projection has been asserted
dc up -d commands-consumer commands-relay >/dev/null

STAFF=$(be python manage.py shell -c "
from django.contrib.auth import get_user_model
from employee import service as employee_service
from accounts.roles import StaffRole, set_staff_role
from orders.models import Order
staff = get_user_model().objects.create_user(username='+79995551111', phone='+79995551111',
                                             password='Pass!2345', customer_version=1)
root = get_user_model().objects.create_superuser(username='+79995559999', password='Pass!2345')
staff = set_staff_role(actor=root, target=staff, role=StaffRole.OPERATOR)
order = Order.objects.get(idempotency_key='e2e-key')
print(staff.id, staff.authz_version, order.id)
" | tail -1 | tr -d '\r')
read -r ACTOR_ID AUTHZ_VERSION ORDER_ID <<<"$STAFF"

ops python manage.py shell -c "
from operations.commands import create_transition_command
command, _ = create_transition_command(
    actor_id=$ACTOR_ID, actor_authz_version=$AUTHZ_VERSION, order_id=$ORDER_ID,
    expected_status='created', target_status='confirmed', idempotency_key='e2e-transition',
)
print('command', command.command_id)
"

wait_for order-confirmed order_confirmed
for _ in 1 2 3 4 5; do be python manage.py publish_outbox; done
wait_for command-succeeded command_succeeded

echo "== reconcile the completed run (expect status ok) =="
ops python manage.py reconcile

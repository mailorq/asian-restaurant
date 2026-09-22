#!/usr/bin/env bash
# operations connection pool on the production render in an isolated compose project: the api runs with a pool of one,
# the authorization projection is locked the way a slow database holds it, and parallel requests signed by identity come in
# every answer must be 200 or 503, never 500 or 401: a request past the worker's bound gets uvicorn's plain 503, one that waited out the pool the neutral json 503
# the api must never hold more than one connection, the saturation counter must match the json 503s, the pids limit is never reached, and no burst leaves threads behind for the next
# reads only tracked config and a throwaway env-file outside the checkout, and tears down only its own project
set -euo pipefail

PROJ="${SMOKE_PROJECT:-ar_opspool_$(date +%s)_$$}"
PY_IMAGE=python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
WORK="$(mktemp -d)"
# docker on windows takes host paths in its own form, and git bash must not rewrite the container side
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
fi
ENV_FILE="$WORK/production.env"

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml -f "$WORK/pool-of-one.yaml" "$@"; }
cleanup() { echo "== teardown (only $PROJ) =="; dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# the only deviation from the production render, and the condition under test
cat > "$WORK/pool-of-one.yaml" <<'EOF'
services:
  operations-api:
    environment:
      OPERATIONS_DB_POOL_MAX_SIZE: "1"
EOF
openssl genrsa -out "$WORK/identity.pem" 2048 2>/dev/null
# the backend reads the secret as uid 10001
chmod 644 "$WORK/identity.pem"
OPS_BOOT=smoke_ops_boot
cat > "$ENV_FILE" <<EOF
POSTGRES_DB=storefront
POSTGRES_USER=smoke_boot
POSTGRES_PASSWORD=$(rnd)
STOREFRONT_DB_MIGRATOR_PASSWORD=$(rnd)
STOREFRONT_DB_RUNTIME_PASSWORD=$(rnd)
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=$OPS_BOOT
OPERATIONS_DB_PASSWORD=$(rnd)
OPERATIONS_DB_MIGRATOR_PASSWORD=$(rnd)
OPERATIONS_DB_RUNTIME_PASSWORD=$(rnd)
DJANGO_SECRET_KEY=smoke-$(rnd)
DJANGO_ALLOWED_HOSTS=smoke.invalid,backend
REDIS_URL=redis://redis:6379/1
CART_REDIS_URL=redis://redis:6379/0
RABBITMQ_URL=amqp://storefront_app:smoke@rabbitmq:5672/storefront
IDENTITY_JWT_KID=smoke-1
IDENTITY_JWT_KEY_FILE=$WORK/identity.pem
RABBITMQ_ADMIN_USER=smoke_admin
RABBITMQ_ADMIN_PASSWORD=$(rnd)
RABBITMQ_ERLANG_COOKIE=$(rnd)
STOREFRONT_MQ_PASSWORD=$(rnd)
OPERATIONS_MQ_PASSWORD=$(rnd)
BRIDGE_MQ_PASSWORD=$(rnd)
OPERATIONS_COMMANDS_MQ_PASSWORD=$(rnd)
OPERATIONS_SECRET_KEY=smoke-$(rnd)
OPERATIONS_ALLOWED_HOSTS=smoke.invalid
OPERATIONS_RABBITMQ_URL=amqp://operations_consumer:smoke@rabbitmq:5672/operations
OPERATIONS_BRIDGE_CONSUME_URL=amqp://operations_bridge:smoke@rabbitmq:5672/storefront
OPERATIONS_BRIDGE_PUBLISH_URL=amqp://operations_bridge:smoke@rabbitmq:5672/operations
OPERATIONS_COMMANDS_RABBITMQ_URL=amqp://operations_commands:smoke@rabbitmq:5672/storefront
EOF

cat > "$WORK/load.py" <<'PY'
import concurrent.futures as cf
import json
import os
import sys
import urllib.error
import urllib.request

HEADERS = {"Host": "smoke.invalid", "Authorization": f"Bearer {os.environ['TOKEN']}"}


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=40) as r:
            return r.status, r.headers.get("Retry-After"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Retry-After"), e.read().decode()


mode = sys.argv[1]
if mode == "one":
    status, _, body = get("http://operations-api:9000/ops-api/orders")
    print(json.dumps({"status": status, "body": body[:120]}))
elif mode == "backend-metrics":
    # no redirect is followed: prometheus has to get the page itself on the plain internal hop
    import http.client
    connection = http.client.HTTPConnection("backend", 8000, timeout=10)
    connection.request("GET", "/metrics")
    response = connection.getresponse()
    print(json.dumps({"status": response.status, "django": b"django_http_requests" in response.read()}))
elif mode == "burst":
    with cf.ThreadPoolExecutor(60) as ex:
        results = list(ex.map(lambda _: get("http://operations-api:9000/ops-api/orders"), range(60)))
    pool = '{"detail": "service temporarily unavailable"}'
    kinds = {"200": 0, "503 pool": 0, "503 bound": 0, "other": 0}
    for status, retry_after, body in results:
        if status == 200:
            kinds["200"] += 1
        elif status == 503 and body == pool and retry_after == "1":
            kinds["503 pool"] += 1
        elif status == 503 and body == "Service Unavailable":
            kinds["503 bound"] += 1
        else:
            kinds["other"] += 1
            kinds.setdefault("other examples", []).append(f"{status} {body[:60]}")
    print(json.dumps(kinds))
else:
    metrics = urllib.request.urlopen("http://operations-api:9105/metrics", timeout=10).read().decode()
    print(sum(float(line.split()[-1]) for line in metrics.splitlines()
              if line.startswith("operations_db_pool_exhausted_total")))
PY
client() { TOKEN="${TOKEN:-}" docker run --rm --network "${PROJ}_default" -e TOKEN -v "$WORK/load.py:/load.py:ro" "$PY_IMAGE" python /load.py "$@"; }

echo "== production stack with an operations pool of one =="
dc up -d --build backend operations-api >"$WORK/up.log" 2>&1 || { tail -30 "$WORK/up.log"; fail "the stack did not start"; }
for _ in $(seq 1 60); do
  dc exec -T operations-api python -c "import urllib.request; urllib.request.urlopen('http://localhost:9105/metrics', timeout=2)" >/dev/null 2>&1 && break
  sleep 2
done

echo "== a token signed by identity and the authorization projection that admits it =="
TOKEN="$(dc exec -T backend python - <<'PY'
import os, time, uuid

import jwt

key = open("/run/secrets/identity_jwt_private_key").read()
now = int(time.time())
claims = {"iss": "identity", "aud": "operations", "sub": "4242", "roles": ["restaurant_manager"], "authz_version": 1,
          "jti": uuid.uuid4().hex, "iat": now, "exp": now + 900}
print(jwt.encode(claims, key, algorithm="RS256", headers={"kid": os.environ["IDENTITY_JWT_KID"]}))
PY
)"
export TOKEN
dc exec -T operations-api python manage.py shell -c "
from operations.models import EmployeeAuthorization
EmployeeAuthorization.objects.update_or_create(subject_id=4242, defaults={'authz_version': 1, 'role_active': True,
    'roles': ['restaurant_manager'], 'roles_known': True, 'user_active': True})" >/dev/null
baseline="$(client one)"
grep -q '"status": 200' <<<"$baseline" || fail "an authorized request does not pass before the test: $baseline"
echo "OK an authorized request passes: identity signs, operations verifies through the backend's jwks"
scrape="$(client backend-metrics)"
[ "$scrape" = '{"status": 200, "django": true}' ] || fail "prometheus cannot scrape the backend on the internal hop: $scrape"
echo "OK the backend's metrics answer 200 on the internal hop, not a redirect to https"

# processes and threads in the api container, the reading cat included, so readings compare like for like
threads() { dc exec -T operations-api cat /sys/fs/cgroup/pids.current; }
burst_under_lock() {  # burst_under_lock <n>: prints the burst result, leaves the connection peak in $WORK/peak<n>
  dc exec -T operations-db psql -X -A -t -q -U "$OPS_BOOT" -d operations \
    -c "select count(*) from pg_stat_activity where usename = 'operations_runtime' and backend_type = 'client backend'" \
    -c '\watch i=0.5 c=50' >"$WORK/samples$1.txt" 2>&1 &
  local sampler=$!
  sleep 1
  dc exec -T operations-db psql -X -q -U "$OPS_BOOT" -d operations \
    -c "begin; lock table operations_employeeauthorization in access exclusive mode; select pg_sleep(12); commit;" >/dev/null &
  local locker=$!
  sleep 1
  client burst
  wait "$locker"
  wait "$sampler" || true
  grep -E '^[0-9]+$' "$WORK/samples$1.txt" | sort -n | tail -1 >"$WORK/peak$1"
}

idle="$(threads)"
# the event loop's default executor keeps up to this many threads once a burst has started them
executor_cap="$(dc exec -T operations-api python -c "import os; print(min(32, (os.cpu_count() or 1) + 4))")"

echo "== twice: the projection locked for 12 s while 60 authorized requests arrive, past the worker's bound of 32 =="
first="$(burst_under_lock 1)"; echo "   $first"
second="$(burst_under_lock 2)"; echo "   $second"
sleep 5
after="$(threads)"
limit_hits="$(dc exec -T operations-api sh -c "sed -n 's/^max //p' /sys/fs/cgroup/pids.events")"

python3 - "$first" "$second" "$(cat "$WORK/peak1" "$WORK/peak2" | sort -n | tail -1)" "$(client metrics)" "$idle" "$executor_cap" "$after" "$limit_hits" <<'PY'
import json, sys
bursts = [json.loads(sys.argv[1]), json.loads(sys.argv[2])]
peak, exhausted = int(sys.argv[3]), float(sys.argv[4])
idle, executor_cap, after, limit_hits = int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7]), int(sys.argv[8])
problems = []
for n, kinds in enumerate(bursts, 1):
    if kinds["other"]:
        problems.append(f"burst {n}: answers other than 200 and the two neutral 503s: {kinds}")
    if not kinds["503 pool"]:
        problems.append(f"burst {n}: no request waited out the pool, so the pool was never tested")
    if not kinds["503 bound"]:
        problems.append(f"burst {n}: no request was turned away at the worker's bound, so the bound was never tested")
    if not kinds["200"]:
        problems.append(f"burst {n}: no request was served once the lock was released")
pool_503 = sum(kinds["503 pool"] for kinds in bursts)
if peak != 1:
    problems.append(f"the api held {peak} connections at its peak, the pool allows 1")
if exhausted != pool_503:
    problems.append(f"operations_db_pool_exhausted_total is {exhausted:g}, the pool 503s were {pool_503}")
if limit_hits:
    problems.append(f"the container hit its pids limit {limit_hits} times")
if after > idle + executor_cap + 2:
    problems.append(f"the worker keeps threads past its executor: {idle} idle, {after} after two bursts, the executor holds at most {executor_cap}")
if problems:
    sys.exit("FAIL: " + "; ".join(problems))
print(f"OK only 200 and neutral 503s in both bursts, peak connections {peak}, saturation counter {exhausted:g} = pool 503s, "
      f"threads {idle} idle and {after} after two bursts within the executor's {executor_cap}, pids limit never hit")
PY

after="$(client one)"
grep -q '"status": 200' <<<"$after" || fail "the pool did not recover after the lock: $after"
echo "OK the pool recovered once the lock was released"
echo "SMOKE OK"

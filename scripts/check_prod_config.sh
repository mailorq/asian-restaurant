#!/usr/bin/env bash
# CI guard: the production compose must render from a clean checkout (no repo-root .env),
# inject the signing key only as a read-only secret file, keep the legacy in-storefront ops
# consumer out of production, and fail fast when any required production variable is missing.
# Wire into CI as a required check.
set -euo pipefail

# a throwaway key file only so `config` can resolve the secret source, and an empty env-file
# so nothing outside the explicit ${VAR:?} set can satisfy a required variable during interpolation
TMPKEY="$(mktemp)"; EMPTY_ENV="$(mktemp)"
trap 'rm -f "$TMPKEY" "$EMPTY_ENV"' EXIT
printf 'dummy\n' > "$TMPKEY"

# every mandatory ${VAR:?} referenced by a service that is part of the default production render
MANDATORY=(
  DJANGO_SECRET_KEY DJANGO_ALLOWED_HOSTS REDIS_URL CART_REDIS_URL RABBITMQ_URL
  IDENTITY_JWT_KID IDENTITY_JWT_KEY_FILE
  POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD STOREFRONT_DB_MIGRATOR_PASSWORD STOREFRONT_DB_RUNTIME_PASSWORD
  OPERATIONS_DB_NAME OPERATIONS_DB_USER OPERATIONS_DB_PASSWORD OPERATIONS_DB_MIGRATOR_PASSWORD OPERATIONS_DB_RUNTIME_PASSWORD
  RABBITMQ_ADMIN_USER RABBITMQ_ADMIN_PASSWORD RABBITMQ_ERLANG_COOKIE
  STOREFRONT_MQ_PASSWORD OPERATIONS_MQ_PASSWORD BRIDGE_MQ_PASSWORD OPERATIONS_COMMANDS_MQ_PASSWORD
  OPERATIONS_SECRET_KEY OPERATIONS_ALLOWED_HOSTS OPERATIONS_RABBITMQ_URL
  OPERATIONS_BRIDGE_CONSUME_URL OPERATIONS_BRIDGE_PUBLISH_URL OPERATIONS_COMMANDS_RABBITMQ_URL
)

# CI dummy values (non-dev) so the render can complete; the negative loop below removes them one
# at a time to prove each is genuinely required
export DJANGO_SECRET_KEY=ci-django-secret
export DJANGO_ALLOWED_HOSTS=app.example.com
export REDIS_URL=redis://redis:6379/1
export CART_REDIS_URL=redis://redis:6379/0
export RABBITMQ_URL=amqp://u:p@rabbitmq:5672/storefront
export IDENTITY_JWT_KID=prod-1 IDENTITY_JWT_KEY_FILE="$TMPKEY"
export POSTGRES_DB=storefront POSTGRES_USER=storefront POSTGRES_PASSWORD=ci-pg
export STOREFRONT_DB_MIGRATOR_PASSWORD=ci-sf-migrator STOREFRONT_DB_RUNTIME_PASSWORD=ci-sf-runtime
export OPERATIONS_DB_NAME=operations OPERATIONS_DB_USER=operations OPERATIONS_DB_PASSWORD=ci-ops-pg
export OPERATIONS_DB_MIGRATOR_PASSWORD=ci-ops-migrator OPERATIONS_DB_RUNTIME_PASSWORD=ci-ops-runtime
export RABBITMQ_ADMIN_USER=ci RABBITMQ_ADMIN_PASSWORD=ci RABBITMQ_ERLANG_COOKIE=ci
export STOREFRONT_MQ_PASSWORD=ci OPERATIONS_MQ_PASSWORD=ci BRIDGE_MQ_PASSWORD=ci
export OPERATIONS_COMMANDS_MQ_PASSWORD=ci
export OPERATIONS_SECRET_KEY=ci-ops-secret OPERATIONS_ALLOWED_HOSTS=ops.example.com
export OPERATIONS_RABBITMQ_URL=amqp://u:p@rabbitmq:5672/operations
export OPERATIONS_BRIDGE_CONSUME_URL=amqp://u:p@rabbitmq:5672/storefront
export OPERATIONS_BRIDGE_PUBLISH_URL=amqp://u:p@rabbitmq:5672/operations
export OPERATIONS_COMMANDS_RABBITMQ_URL=amqp://u:p@rabbitmq:5672/storefront

PROD=(--env-file "$EMPTY_ENV" -f compose.yaml -f compose.prod.yaml)
rendered=$(docker compose "${PROD[@]}" config)
rendered_json=$(docker compose "${PROD[@]}" config --format json)
fail=0

# no dev fallbacks / no dev signing material / secret injected as a file / no inline key
grep -q "ops-dev-insecure" <<<"$rendered" && { echo "FAIL: ops-dev-insecure fallback present"; fail=1; }
grep -Eq "dev-(storefront|operations)-|ops_user|ops_pass" <<<"$rendered" && { echo "FAIL: dev database credentials fallback present"; fail=1; }
grep -q "definitions.dev.json" <<<"$rendered" && { echo "FAIL: dev RabbitMQ definitions mounted"; fail=1; }
grep -q "jwt_private_key.pem" <<<"$rendered" && { echo "FAIL: dev signing-key mount present"; fail=1; }
grep -q "identity_jwt_private_key" <<<"$rendered" || { echo "FAIL: signing key not injected as a secret"; fail=1; }
grep -Eq "IDENTITY_JWT_PRIVATE_KEY:[[:space:]]*[^\"'[:space:]]" <<<"$rendered" && { echo "FAIL: inline private key in env"; fail=1; }

# legacy in-storefront projection consumer must not run in production
grep -q "run_ops_consumer" <<<"$rendered" && { echo "FAIL: legacy ops consumer present in production"; fail=1; }

grep -B3 -A3 'published: "9090"' <<<"$rendered" | grep -q 'host_ip: 127.0.0.1' || {
  echo "FAIL: prometheus 9090 not bound to loopback"; fail=1; }

# nothing may be reachable from outside the host: TLS is terminated by an ingress in front of this stack, so a port published on 0.0.0.0 would serve the app in the clear
RENDERED_JSON="$rendered_json" python3 - <<'PORTS' || fail=1
import json, os

exposed = [
    f"{name}:{p.get('published')}"
    for name, spec in sorted(json.loads(os.environ["RENDERED_JSON"])["services"].items())
    for p in (spec.get("ports") or [])
    if p.get("host_ip") not in ("127.0.0.1", "::1")
]
if exposed:
    print("FAIL: published outside loopback: " + ", ".join(exposed))
    raise SystemExit(1)
PORTS

# the ingress terminates TLS and the hop to nginx is plain http, so overwriting these two turns the redirect below into a loop and collapses every client into one rate-limit bucket
PROXY_PARAMS=frontend/nginx/proxy_params.conf
grep -Eq 'proxy_set_header[[:space:]]+X-Forwarded-Proto[[:space:]]+\$scheme;' "$PROXY_PARAMS" && {
  echo "FAIL: X-Forwarded-Proto forced to \$scheme; the ingress value is lost"; fail=1; }
grep -Eq 'proxy_set_header[[:space:]]+X-Forwarded-For[[:space:]]+\$proxy_add_x_forwarded_for;' "$PROXY_PARAMS" || {
  echo "FAIL: X-Forwarded-For does not append to the chain the ingress sent"; fail=1; }
grep -Eq 'map[[:space:]]+\$http_x_forwarded_proto[[:space:]]+\$forwarded_proto' frontend/nginx/default.conf || {
  echo "FAIL: \$forwarded_proto is not derived from the header the ingress sent"; fail=1; }
grep -A4 'map[[:space:]]\+\$http_x_forwarded_proto' frontend/nginx/default.conf | grep -q '\$scheme' || {
  echo "FAIL: no scheme fallback for a request that arrives without a proxy"; fail=1; }

# every answer the spa host gives carries the same security headers, and add_header inside a location replaces what an outer block set, so each location has to include them
python3 - <<'HEADERS' || fail=1
import pathlib, re

conf = pathlib.Path("frontend/nginx/default.conf").read_text(encoding="utf-8")
headers = pathlib.Path("frontend/nginx/security_headers.conf").read_text(encoding="utf-8")
failed = []
for what, pattern in (("X-Content-Type-Options nosniff", r'X-Content-Type-Options "nosniff"'),
                      ("a content security policy", r"Content-Security-Policy"),
                      ("frame-ancestors 'none'", r"frame-ancestors 'none'"),
                      ("a referrer policy", r"Referrer-Policy")):
    if not re.search(pattern, headers):
        failed.append("security_headers.conf is missing " + what)
bare = [block.splitlines()[0].strip() for block in re.findall(r"location [^{]+\{[^}]*\}", conf, re.S)
        if "security_headers.conf" not in block]
if bare:
    failed.append("these locations answer without the security headers: " + "; ".join(bare))
if "server_tokens off;" not in conf:
    failed.append("nginx announces its version")
if not re.search(r"location = /admin/login/ \{[^}]*limit_req zone=", conf, re.S):
    failed.append("the admin password form is not rate limited")
if failed:
    print("FAIL: " + "; ".join(failed))
    raise SystemExit(1)
HEADERS

# migrations are a deployment step, not something every process races on startup
RENDERED_JSON="$rendered_json" python3 - <<'MIGRATE' || fail=1
import json, os

GUARD = {"backend/Dockerfile": "storefront", "services/order_operations/Dockerfile": "operations"}
services = json.loads(os.environ["RENDERED_JSON"])["services"]
migrators, runtime_migrating = {}, []
for name, spec in sorted(services.items()):
    side = GUARD.get((spec.get("build") or {}).get("dockerfile", ""))
    if not side:
        continue
    env = spec.get("environment") or {}
    runs = str(env.get("RUN_MIGRATIONS", "")) == "1"
    if runs:
        migrators.setdefault(side, []).append(name)
    if runs and spec.get("restart") not in (None, "no"):
        runtime_migrating.append(name)
for side in sorted(GUARD.values()):
    found = migrators.get(side, [])
    if len(found) != 1:
        print(f"FAIL: {side} needs exactly one migration service, found {found or 'none'}")
        raise SystemExit(1)
if runtime_migrating:
    print("FAIL: a long-running service migrates on startup: " + ", ".join(runtime_migrating))
    raise SystemExit(1)

# the migrator must run once and exit, or the services waiting on it never start
for side, (job,) in ((side, migrators[side]) for side in sorted(migrators)):
    spec = services[job]
    if spec.get("restart") not in (None, "no") or spec.get("command") != ["true"]:
        print(f"FAIL: {job} is not a one-shot job (restart={spec.get('restart')!r}, "
              f"command={spec.get('command')!r})")
        raise SystemExit(1)

# every runtime service must wait for its own migrator: without this it can start against a
# schema the deployment has not migrated yet
unguarded = []
for name, spec in sorted(services.items()):
    side = GUARD.get((spec.get("build") or {}).get("dockerfile", ""))
    if not side or name in migrators.get(side, []):
        continue
    job = migrators[side][0]
    wait = (spec.get("depends_on") or {}).get(job) or {}
    if wait.get("condition") != "service_completed_successfully":
        unguarded.append(f"{name} -> {job}")
if unguarded:
    print("FAIL: these start without waiting for their migration job: " + ", ".join(unguarded))
    raise SystemExit(1)
MIGRATE

# application images run production as 10001, compose keeps it, and only backend and media-init write media
RENDERED_JSON="$rendered_json" python3 - <<'NONROOT' || fail=1
import json, os, pathlib, re

APP = ("backend/Dockerfile", "services/order_operations/Dockerfile")
failed = []
for dockerfile in APP:
    text = pathlib.Path(dockerfile).read_text(encoding="utf-8")
    stage = re.search(r"^FROM \S+ AS prod\s*$(.*?)(?=^FROM |\Z)", text, re.M | re.S)
    users = re.findall(r"^USER\s+(\S+)\s*$", stage.group(1), re.M) if stage else []
    if users[-1:] != ["10001:10001"]:
        failed.append(f"{dockerfile}: the prod stage does not run as 10001:10001")

services = json.loads(os.environ["RENDERED_JSON"])["services"]
for name, spec in sorted(services.items()):
    if (spec.get("build") or {}).get("dockerfile") in APP and spec.get("user"):
        failed.append(f"{name}: user {spec['user']!r} overrides the image user")

writers = sorted({
    name for name, spec in services.items() for v in spec.get("volumes") or []
    if v.get("type") == "volume" and v.get("source") == "media" and not v.get("read_only")})
if writers != ["backend", "media-init"]:
    failed.append(f"media is writable by {writers}, expected backend and media-init")

init = services.get("media-init") or {}
mounts = [v.get("target") for v in init.get("volumes") or [] if v.get("source") == "media"]
if (len(mounts) != 1 or len(init.get("volumes") or []) != 1
        or init.get("entrypoint") != ["chown", "-R", "10001:10001", *mounts]
        or init.get("command") or init.get("restart") not in (None, "no")
        or init.get("network_mode") != "none" or init.get("user") != "0:0"):
    failed.append("media-init is not a root one-shot chown of the media volume alone, with no network")
wait = ((services.get("backend") or {}).get("depends_on") or {}).get("media-init") or {}
if wait.get("condition") != "service_completed_successfully":
    failed.append("backend starts without waiting for media-init")

if failed:
    print("FAIL: " + "; ".join(failed))
    raise SystemExit(1)
NONROOT

# application processes run read-only without capabilities or privilege gain, with bounded memory and process count
RENDERED_JSON="$rendered_json" python3 - <<'HARDEN' || fail=1
import json, os

APP = ("backend/Dockerfile", "services/order_operations/Dockerfile")
NNP = {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
services = json.loads(os.environ["RENDERED_JSON"])["services"]


def gaps(spec, cap_add):
    found = []
    if spec.get("read_only") is not True:
        found.append("read_only")
    if spec.get("cap_drop") != ["ALL"] or sorted(spec.get("cap_add") or []) != cap_add:
        found.append("capabilities")
    if not NNP & set(spec.get("security_opt") or []):
        found.append("no-new-privileges")
    if not spec.get("mem_limit") or not spec.get("pids_limit"):
        found.append("limits")
    return found


failed = []
for name, spec in sorted(services.items()):
    if (spec.get("build") or {}).get("dockerfile") not in APP:
        continue
    found = gaps(spec, [])
    mounts = {t.split(":", 1)[0] for t in spec.get("tmpfs") or []}
    wanted = {"/tmp", (spec.get("environment") or {}).get("PROMETHEUS_MULTIPROC_DIR") or "/tmp"}
    if not wanted <= mounts:
        found.append("tmpfs " + ", ".join(sorted(wanted - mounts)))
    if found:
        failed.append(f"{name}: {', '.join(found)}")
found = gaps(services.get("media-init") or {}, ["CHOWN", "DAC_READ_SEARCH"])
if found:
    failed.append("media-init: " + ", ".join(found))
for name in ("storefront-db-provision", "operations-db-provision"):
    spec = services.get(name) or {}
    found = gaps(spec, [])
    if str(spec.get("user") or "0").split(":")[0] in ("0", "root"):
        found.append("runs as root")
    if found:
        failed.append(f"{name}: {', '.join(found)}")
if failed:
    print("FAIL: not hardened: " + "; ".join(failed))
    raise SystemExit(1)
HARDEN

# per database, every application process holds at most its own pool: the sum, plus one manage.py run through dc exec, stays at half of max_connections or less
RENDERED_JSON="$rendered_json" python3 - <<'DBPOOL' || fail=1
import json, os, pathlib, re

services = json.loads(os.environ["RENDERED_JSON"])["services"]
deploy = pathlib.Path("DEPLOY.md").read_text(encoding="utf-8")
SIDES = (
    ("storefront", "backend/Dockerfile", "backend/config/settings.py", "DJANGO_DB_POOL_MAX_SIZE", "db", "backend"),
    ("operations", "services/order_operations/Dockerfile", "services/order_operations/config/settings.py",
     "OPERATIONS_DB_POOL_MAX_SIZE", "operations-db", "operations-api"),
)
failed = []
for side, dockerfile, settings, var, db, exec_service in SIDES:
    default = int(re.search(rf'"{var}", default=(\d+)', pathlib.Path(settings).read_text(encoding="utf-8")).group(1))
    workers = re.search(r'"--workers", "(\d+)"', pathlib.Path(dockerfile).read_text(encoding="utf-8"))
    workers = int(workers.group(1)) if workers else 1  # the gunicorn default
    configured = re.search(r"max_connections=(\d+)", " ".join(services[db].get("command") or []))
    limit = int(configured.group(1)) if configured else 100  # the postgres default
    budget, parts = 0, []
    for name, spec in sorted(services.items()):
        if (spec.get("build") or {}).get("dockerfile") != dockerfile:
            continue
        size = int((spec.get("environment") or {}).get(var) or default)
        processes = workers if spec.get("command") is None else 1
        budget += processes * size
        parts.append(f"{name} {processes}x{size}")
    exec_size = int((services[exec_service].get("environment") or {}).get(var) or default)
    budget += exec_size
    parts.append(f"dc exec 1x{exec_size}")
    detail = f"{side} database budget {budget} of {limit} ({', '.join(parts)})"
    if budget > limit // 2:
        failed.append(detail + " is over half of max_connections")
    # the runbook states the same sum, counted over the same production render
    stated = re.search(rf"соединений {side}:.*?=\s+(\d+)\s+из\s+(\d+)\s+`max_connections`", deploy, re.S)
    if not stated or (int(stated.group(1)), int(stated.group(2))) != (budget, limit):
        failed.append(detail + ", DEPLOY.md states " + (f"{stated.group(1)} of {stated.group(2)}" if stated else "none"))
if failed:
    print("FAIL: " + "; ".join(failed))
    raise SystemExit(1)
DBPOOL

# every database has three roles: the bootstrap superuser only provisions, the migrator runs migrations, every other process connects as the runtime role
RENDERED_JSON="$rendered_json" python3 - <<'DBROLES' || fail=1
import json, os
from urllib.parse import urlsplit

services = json.loads(os.environ["RENDERED_JSON"])["services"]
SIDES = (
    ("storefront", "backend/Dockerfile", "DATABASE_URL", "db"),
    ("operations", "services/order_operations/Dockerfile", "OPERATIONS_DATABASE_URL", "operations-db"),
)
failed = []
for side, dockerfile, key, db in SIDES:
    bootstrap = services[db]["environment"]["POSTGRES_USER"]
    database = services[db]["environment"]["POSTGRES_DB"]
    provision = f"{side}-db-provision"
    env = (services.get(provision) or {}).get("environment") or {}
    if (env.get("PGHOST"), env.get("PGUSER"), env.get("PGDATABASE"), env.get("DB_MIGRATOR_ROLE"), env.get("DB_RUNTIME_ROLE")) \
            != (db, bootstrap, database, f"{side}_migrator", f"{side}_runtime"):
        failed.append(f"{provision} does not provision {side}_migrator and {side}_runtime on {db}/{database} as the bootstrap user")
    # production mode also holds the bootstrap password to the production rules
    if str(env.get("DB_PRODUCTION", "")) != "1":
        failed.append(f"{provision} does not run in production mode")
    for name, spec in sorted(services.items()):
        if (spec.get("build") or {}).get("dockerfile") != dockerfile:
            continue
        env = spec.get("environment") or {}
        migrator = str(env.get("RUN_MIGRATIONS", "")) == "1"
        want = f"{side}_migrator" if migrator else f"{side}_runtime"
        dsn = urlsplit(str(env.get(key, "")))
        if (dsn.username, dsn.hostname, dsn.path.lstrip("/")) != (want, db, database):
            failed.append(f"{name} connects as {dsn.username} to {dsn.hostname}/{dsn.path.lstrip('/')}, expected {want} on {db}/{database}")
        wait = (spec.get("depends_on") or {}).get(provision) or {}
        if migrator and wait.get("condition") != "service_completed_successfully":
            failed.append(f"{name} migrates without waiting for {provision}")
if failed:
    print("FAIL: " + "; ".join(failed))
    raise SystemExit(1)
DBROLES

# under ASGI a request in flight holds a thread and, as it ends, the one that joins it; the worker's bound caps them, and the pids limit must cover them plus the event loop's default executor (at most 32) and a few of the worker's own
RENDERED_JSON="$rendered_json" python3 - <<'THREADS' || fail=1
import json, os, pathlib, re

WORKERS = {"backend/Dockerfile": "backend/config/workers.py",
           "services/order_operations/Dockerfile": "services/order_operations/config/workers.py"}
failed = []
for name, spec in sorted(json.loads(os.environ["RENDERED_JSON"])["services"].items()):
    dockerfile = (spec.get("build") or {}).get("dockerfile")
    if dockerfile not in WORKERS or spec.get("command") is not None:
        continue
    image = pathlib.Path(dockerfile).read_text(encoding="utf-8")
    if '"config.workers.BoundedUvicornWorker"' not in image:
        failed.append(f"{dockerfile} does not run the bounded worker")
        continue
    bound = int(re.search(r'"limit_concurrency": (\d+)', pathlib.Path(WORKERS[dockerfile]).read_text(encoding="utf-8")).group(1))
    workers = re.search(r'"--workers", "(\d+)"', image)
    workers = int(workers.group(1)) if workers else 1  # the gunicorn default
    need = workers * (2 * bound + 32 + 8) + 1
    if int(spec.get("pids_limit") or 0) < need:
        failed.append(f"{name}: pids_limit {spec.get('pids_limit')} is below {need} for {workers} workers of {bound} requests")
if failed:
    print("FAIL: " + "; ".join(failed))
    raise SystemExit(1)
THREADS

# the release procedure must render the production stack: a bare `docker compose` there brings up the dev overlay with its mounts and fallbacks
python3 - <<'DEPLOYDOC' || fail=1
import pathlib, re

doc = pathlib.Path("DEPLOY.md").read_text(encoding="utf-8")
match = re.search(r"^dc\(\)\s*\{\n(.*?)^\}", doc, re.M | re.S)
if not match:
    print("FAIL: DEPLOY.md defines no dc() wrapper")
    raise SystemExit(1)
body = match.group(1)
required = {
    "the compose call reads the resolved file with both compose files":
        r'docker compose --env-file "\$env_file" -f compose\.yaml -f compose\.prod\.yaml',
    "an unset PROD_ENV_FILE refuses":
        r'\$\{PROD_ENV_FILE:\?',
    "an unreadable PROD_ENV_FILE refuses the call":
        r'test -r "\$PROD_ENV_FILE" \|\|.*return 1',
    "the checkout root is resolved":
        r'repo_root="\$\(realpath \.\)"',
    "PROD_ENV_FILE is resolved before it is judged":
        r'env_file="\$\(realpath "\$PROD_ENV_FILE"\)"',
    "a file resolving inside the checkout refuses the call":
        r'case "\$env_file" in\s*\n\s*"\$repo_root"/\*\)[^\n]*return 1',
}
missing = [what for what, pattern in required.items() if not re.search(pattern, body, re.S)]
if missing:
    print("FAIL: dc() in DEPLOY.md is missing: " + "; ".join(missing))
    raise SystemExit(1)
outside = [ln.strip() for ln in doc.replace(match.group(0), "").splitlines() if "docker compose" in ln]
if outside:
    print("FAIL: DEPLOY.md calls docker compose outside dc(): " + "; ".join(outside))
    raise SystemExit(1)
# rabbitmq-provision sits in a profile, so a plain `dc up -d` never grants the users and permissions
release = [ln.strip() for ln in doc.splitlines() if ln.startswith("dc ")]
steps = ["dc up -d rabbitmq", "dc --profile provision run --rm rabbitmq-provision",
         "dc up --exit-code-from storefront-migrate storefront-migrate",
         "dc up --exit-code-from operations-migrate operations-migrate", "dc up -d"]
at = [release.index(step) if step in release else -1 for step in steps]
if -1 in at or at != sorted(at):
    print("FAIL: DEPLOY.md does not provision the broker before migrations and the runtime, in this order: "
          + "; ".join(steps))
    raise SystemExit(1)
DEPLOYDOC

# HTTPS redirect must be on by default in production, not left to the operator to remember
grep -Eq "DJANGO_SSL_REDIRECT:[[:space:]]*[\"']?(1|true|True)[\"']?" <<<"$rendered" || {
  echo "FAIL: DJANGO_SSL_REDIRECT not enabled in the production render"; fail=1; }

# every Django process must start under its production guard. keyed off the image a service is
# built from rather than a service count, so a new one cannot satisfy the check by merely existing
RENDERED_JSON="$rendered_json" python3 - <<'GUARD' || fail=1
import json, os

GUARD = {"backend/Dockerfile": "DJANGO_PRODUCTION",
         "services/order_operations/Dockerfile": "OPERATIONS_PRODUCTION"}
missing = []
checked = set()
for name, spec in sorted(json.loads(os.environ["RENDERED_JSON"])["services"].items()):
    guard = GUARD.get((spec.get("build") or {}).get("dockerfile", ""))
    if not guard:
        continue
    checked.add(guard)
    if guard not in (spec.get("environment") or {}):
        missing.append(name + " (" + guard + ")")
if missing:
    print("FAIL: production guard missing on: " + ", ".join(missing))
    raise SystemExit(1)
unmatched = sorted(set(GUARD.values()) - checked)
if unmatched:
    print("FAIL: no service matched the guard for: " + ", ".join(unmatched))
    raise SystemExit(1)
GUARD
grep -Eq "OPERATIONS_ALLOWED_HOSTS:[[:space:]]*'?\*'?[[:space:]]*$" <<<"$rendered" && { echo "FAIL: wildcard ALLOWED_HOSTS"; fail=1; }

# each mandatory variable being unset must make `config` fail (fail-closed, no silent default)
for var in "${MANDATORY[@]}"; do
  if env -u "$var" docker compose "${PROD[@]}" config >/dev/null 2>&1; then
    echo "FAIL: missing $var did not error"; fail=1
  fi
done

[ "$fail" -eq 0 ] && echo "prod config OK"
exit "$fail"

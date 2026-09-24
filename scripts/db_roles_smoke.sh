#!/usr/bin/env bash
# database roles on the production render in an isolated compose project:
# a storefront volume laid out before the split, its tables created by the bootstrap superuser, and a fresh operations volume
# each is provisioned and migrated exactly as a release does it, then the roles are checked in the catalog and by behaviour
# the first release after the split is modelled too: it is refused while an older process still holds a bootstrap session, and the old dev bootstrap password of operations is refused until rotated
# reads only tracked config and a throwaway env-file outside the checkout, and tears down only its own project
set -euo pipefail

PROJ="${SMOKE_PROJECT:-ar_dbroles_$(date +%s)_$$}"
PG_IMAGE=postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
WORK="$(mktemp -d)"
# docker on windows takes host paths in its own form, and git bash must not rewrite the container side
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  WORK="$(cygpath -m "$WORK")"
fi
ENV_FILE="$WORK/production.env"

dc() { docker compose -p "$PROJ" --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml "$@"; }
cleanup() { echo "== teardown (only $PROJ) =="; docker rm -f "${PROJ}_stale" >/dev/null 2>&1 || true; dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

SF_BOOT=smoke_boot SF_BOOT_PW="$(rnd)" SF_MIGRATOR_PW="$(rnd)" SF_RUNTIME_PW="$(rnd)"
# the credentials operations-db used to fall back to in production
OPS_BOOT=ops_user OPS_BOOT_PW=ops_pass OPS_MIGRATOR_PW="$(rnd)" OPS_RUNTIME_PW="$(rnd)"
printf 'throwaway\n' > "$WORK/identity.pem"
cat > "$ENV_FILE" <<EOF
POSTGRES_DB=storefront
POSTGRES_USER=$SF_BOOT
POSTGRES_PASSWORD=$SF_BOOT_PW
STOREFRONT_DB_MIGRATOR_PASSWORD=$SF_MIGRATOR_PW
STOREFRONT_DB_RUNTIME_PASSWORD=$SF_RUNTIME_PW
OPERATIONS_DB_NAME=operations
OPERATIONS_DB_USER=$OPS_BOOT
OPERATIONS_DB_PASSWORD=$OPS_BOOT_PW
OPERATIONS_DB_MIGRATOR_PASSWORD=$OPS_MIGRATOR_PW
OPERATIONS_DB_RUNTIME_PASSWORD=$OPS_RUNTIME_PW
DJANGO_SECRET_KEY=smoke-$(rnd)
DJANGO_ALLOWED_HOSTS=smoke.invalid
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

# catalog query as the bootstrap superuser, over the server's own socket
catalog() { dc exec -T "$1" psql -X -A -t -q -U "$2" -d "$3" -c "$4"; }
# a client on the project network, authenticating with a password like the application does
client() {  # client <password> <host> <user> <db> <sql>
  PGPASSWORD="$1" docker run --rm --network "${PROJ}_default" -e PGPASSWORD "$PG_IMAGE" \
    psql -X -q -h "$2" -U "$3" -d "$4" -v ON_ERROR_STOP=1 -c '\set VERBOSITY verbose' -c "$5" 2>&1
}
wait_ready() {
  for _ in $(seq 1 60); do dc exec -T "$1" pg_isready -q >/dev/null 2>&1 && return 0; sleep 2; done
  fail "$1 never became ready"
}

echo "== storefront volume from before the split: the bootstrap superuser migrates it =="
dc up -d db >/dev/null
wait_ready db
dc run --rm --no-deps -e DATABASE_URL="postgres://$SF_BOOT:$SF_BOOT_PW@db:5432/storefront" storefront-migrate >/dev/null 2>&1 \
  || fail "the pre-split migration as the bootstrap superuser failed"
legacy="$(catalog db "$SF_BOOT" storefront "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' and c.relowner = '$SF_BOOT'::regrole")"
[ "$legacy" -gt 0 ] || fail "the pre-split layout has no tables owned by the bootstrap superuser"
echo "OK $legacy storefront tables owned by the bootstrap superuser"

echo "== an older release still connected as the bootstrap superuser: provisioning refuses and changes nothing =="
# an idle connection, like one an old process keeps in its pool
PGPASSWORD="$SF_BOOT_PW" docker run -d --name "${PROJ}_stale" --network "${PROJ}_default" -e PGPASSWORD "$PG_IMAGE" \
  sh -c "sleep 600 | psql -h db -U $SF_BOOT -d storefront" >/dev/null
sleep 4
if dc up --exit-code-from storefront-migrate storefront-migrate >/dev/null 2>&1; then
  fail "provisioning ran beside an open bootstrap session"
fi
dc logs storefront-db-provision >"$WORK/refused.log" 2>&1
grep -q "open sessions of the bootstrap superuser" "$WORK/refused.log" || { tail -20 "$WORK/refused.log"; fail "the refusal does not name the open sessions"; }
[ "$(catalog db "$SF_BOOT" storefront "select count(*) from pg_roles where rolname in ('storefront_migrator', 'storefront_runtime')")" = 0 ] \
  || fail "provisioning changed roles before refusing"
docker rm -f "${PROJ}_stale" >/dev/null
for _ in $(seq 1 30); do
  [ "$(catalog db "$SF_BOOT" storefront "select count(*) from pg_stat_activity where usename = current_user and backend_type = 'client backend' and pid <> pg_backend_pid()")" = 0 ] && break
  sleep 1
done
echo "OK provisioning refused beside an open bootstrap session and left the roles untouched"

echo "== release: provisioning as a dependency of each migration job, then the migrations =="
dc up --exit-code-from storefront-migrate storefront-migrate >"$WORK/storefront-release.log" 2>&1 \
  || { tail -20 "$WORK/storefront-release.log"; fail "the storefront release migration failed"; }

echo "== operations volume with the old dev bootstrap password: production provisioning refuses it =="
if dc up --exit-code-from operations-migrate operations-migrate >/dev/null 2>&1; then
  fail "production provisioning accepted the dev bootstrap password"
fi
dc logs operations-db-provision >"$WORK/weak.log" 2>&1
grep -q "production needs a bootstrap password" "$WORK/weak.log" || { tail -20 "$WORK/weak.log"; fail "the refusal does not name the weak bootstrap password"; }
echo "== one-off bootstrap rotation, as DEPLOY.md runs it =="
NEW_BOOTSTRAP_PASSWORD="$(rnd)" && export NEW_BOOTSTRAP_PASSWORD
dc exec -T -e NEW_BOOTSTRAP_PASSWORD operations-db psql -X -q -U ops_user -d operations -f - < ops/postgres/rotate_bootstrap.sql >/dev/null \
  || fail "the bootstrap rotation failed"
OPS_BOOT_PW="$NEW_BOOTSTRAP_PASSWORD"
unset NEW_BOOTSTRAP_PASSWORD
sed -i "s/^OPERATIONS_DB_PASSWORD=.*/OPERATIONS_DB_PASSWORD=$OPS_BOOT_PW/" "$ENV_FILE"
out="$(client ops_pass operations-db ops_user operations "select 1" || true)"
grep -q "password authentication failed" <<<"$out" || fail "the old bootstrap password still authenticates"
echo "OK the dev bootstrap password was refused, then rotated, and no longer authenticates"
dc up --exit-code-from operations-migrate operations-migrate >"$WORK/operations-release.log" 2>&1 \
  || { tail -20 "$WORK/operations-release.log"; fail "the operations release migration failed"; }
echo "== provisioning again converges =="
dc run --rm storefront-db-provision >/dev/null || fail "a second storefront-db-provision run failed"
dc run --rm operations-db-provision >/dev/null || fail "a second operations-db-provision run failed"

check_side() {  # check_side <service> <bootstrap> <db> <side> <migrator password> <runtime password>
  local svc="$1" boot="$2" db="$3" side="$4" mpw="$5" rpw="$6"
  local m="${side}_migrator" r="${side}_runtime"
  q() { catalog "$svc" "$boot" "$db" "$1"; }
  [ "$(q "select count(*) from pg_roles where rolname in ('$m', '$r') and rolcanlogin and not (rolsuper or rolcreatedb or rolcreaterole or rolreplication or rolbypassrls)")" = 2 ] \
    || fail "$side roles are not plain login roles"
  [ "$(q "select count(*) from pg_auth_members a join pg_roles r on r.oid = a.member where r.rolname in ('$m', '$r')")" = 0 ] \
    || fail "$side roles are members of other roles"
  [ "$(q "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind in ('r', 'p', 'v', 'm', 'f') and c.relowner <> '$m'::regrole")" = 0 ] \
    || fail "$side has relations the migrator does not own"
  [ "$(q "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
          where n.nspname = 'public' and c.relkind in ('r', 'p')
            and (not (has_table_privilege('$r', c.oid, 'SELECT') and has_table_privilege('$r', c.oid, 'INSERT')
                      and has_table_privilege('$r', c.oid, 'UPDATE') and has_table_privilege('$r', c.oid, 'DELETE'))
                 or has_table_privilege('$r', c.oid, 'TRUNCATE') or has_table_privilege('$r', c.oid, 'REFERENCES')
                 or has_table_privilege('$r', c.oid, 'TRIGGER'))")" = 0 ] \
    || fail "$side runtime role lacks DML or holds more than DML on some table"
  [ "$(q "select has_schema_privilege('$r', 'public', 'USAGE') and not has_schema_privilege('$r', 'public', 'CREATE') and nspowner <> '$r'::regrole and has_database_privilege('$r', current_database(), 'CONNECT') and not has_database_privilege('$r', current_database(), 'CREATE') and not has_database_privilege('$r', current_database(), 'TEMP') and not has_database_privilege('$r', 'postgres', 'CONNECT') from pg_namespace where nspname = 'public'")" = t ] \
    || fail "$side runtime role has the wrong schema or database privileges"
  echo "OK $side catalog: plain login roles, the migrator owns every relation, the runtime role holds DML only"

  local statement out
  for statement in "create table probe (id int)" "alter table django_migrations add column probe int" \
                   "drop table django_migrations" "truncate django_migrations" "create schema probe" \
                   "create temp table probe (id int)" "create role probe" "create database probe"; do
    out="$(client "$rpw" "$svc" "$r" "$db" "$statement" || true)"
    grep -q "ERROR:  42501" <<<"$out" || fail "$r was not refused: $statement ($out)"
  done
  out="$(client "$rpw" "$svc" "$r" postgres "select 1" || true)"
  grep -q 'permission denied for database "postgres"' <<<"$out" || fail "$r reached the postgres database ($out)"
  client "$rpw" "$svc" "$r" "$db" "begin; insert into django_migrations (app, name, applied) values ('smoke', 'probe', now()); update django_migrations set name = 'probe2' where app = 'smoke'; delete from django_migrations where app = 'smoke'; rollback;" >/dev/null \
    || fail "$r cannot read and write rows"
  client "$mpw" "$svc" "$m" "$db" "begin; alter table django_migrations add column probe int; rollback;" >/dev/null \
    || fail "$m cannot alter a table it should own"
  echo "OK $side behaviour: $r is refused DDL, TRUNCATE, TEMP, role and database creation and other databases, and keeps DML; $m alters its tables"
}
check_side db "$SF_BOOT" storefront storefront "$SF_MIGRATOR_PW" "$SF_RUNTIME_PW"
check_side operations-db "$OPS_BOOT" operations operations "$OPS_MIGRATOR_PW" "$OPS_RUNTIME_PW"

echo "== the application images, through the DSNs the production render gives them =="
dc run --rm --no-deps backend python manage.py migrate --check >/dev/null || fail "backend cannot read its migration state"
dc run --rm --no-deps operations-api python manage.py migrate --check >/dev/null || fail "operations-api cannot read its migration state"
for svc in backend operations-api; do
  # only the privilege refusal counts, not a settings or connection error
  out="$(dc run --rm --no-deps "$svc" python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.db import connection
with connection.cursor() as cur:
    cur.execute('create table probe (id int)')
print('created')
" 2>&1 || true)"
  grep -q "permission denied for schema public" <<<"$out" || fail "$svc was not refused CREATE TABLE through its runtime DSN: $(tail -3 <<<"$out")"
done
echo "OK the runtime DSNs read the migration state and cannot create a table"

echo "SMOKE OK"

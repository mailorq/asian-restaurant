#!/usr/bin/env bash
# two releases in a row, each run exactly as the release block of DEPLOY.md is written, in an isolated compose project:
# the second must migrate with and start the processes of its own commit, not the images the first one left behind,
# and nothing the checkout holds beyond that commit may reach its images.
# works on a throwaway git checkout of HEAD outside the repository, publishes what a release publishes (127.0.0.1:80
# and 127.0.0.1:9090), and tears down only its own project and images
set -euo pipefail

PROJ="${SMOKE_PROJECT:-ar_release_$(date +%s)_$$}"
WORK="$(mktemp -d)"
CHECKOUT="$WORK/checkout"
ENV_FILE="$WORK/production.env"
# the release block names no project: compose.yaml would put it into the one a live stack uses
export COMPOSE_PROJECT_NAME="$PROJ" PROD_ENV_FILE="$ENV_FILE"

revisions=()
dc() { (cd "$CHECKOUT" && SOURCE_REVISION="$(git rev-parse HEAD)" docker compose --env-file "$ENV_FILE" -f compose.yaml -f compose.prod.yaml "$@"); }
cleanup() {
  echo "== teardown (only $PROJ) =="
  dc --profile provision down -v --remove-orphans >/dev/null 2>&1 || true
  for revision in "${revisions[@]}"; do
    docker image rm "asian-restaurant/storefront:$revision" "asian-restaurant/operations:$revision" \
      "asian-restaurant/frontend:$revision" >/dev/null 2>&1 || true
  done
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
rnd() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

awk '/^## Порядок релиза/ { section = 1 } section && /^```bash$/ { inside = 1; next } inside && /^```$/ { exit } inside' \
  DEPLOY.md > "$WORK/release.sh"
grep -q '^dc build$' "$WORK/release.sh" || fail "the release block of DEPLOY.md was not found"
sed -n '/^dc() {$/,/^}$/p' "$WORK/release.sh" > "$WORK/dc.sh"
release() { (cd "$CHECKOUT" && bash -euo pipefail "$WORK/release.sh") >"$WORK/release-$1.log" 2>&1 || { tail -30 "$WORK/release-$1.log"; fail "release $1 failed"; }; }

mkdir "$CHECKOUT"
git archive HEAD | tar -x -C "$CHECKOUT"
git -C "$CHECKOUT" init -q
git -C "$CHECKOUT" add -A
git -C "$CHECKOUT" -c user.name=smoke -c user.email=smoke@example.invalid commit -qm first
revisions+=("$(git -C "$CHECKOUT" rev-parse HEAD)")

openssl genrsa -out "$WORK/identity.pem" 2048 2>/dev/null
# read inside the container as 10001
chmod 644 "$WORK/identity.pem"
STOREFRONT_MQ_PASSWORD="$(rnd)" OPERATIONS_MQ_PASSWORD="$(rnd)" BRIDGE_MQ_PASSWORD="$(rnd)" OPERATIONS_COMMANDS_MQ_PASSWORD="$(rnd)"
SF_BOOT=smoke_boot
cat > "$ENV_FILE" <<EOF
POSTGRES_DB=storefront
POSTGRES_USER=$SF_BOOT
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

echo "== first release, as DEPLOY.md runs it =="
release first

echo "== a commit that adds a migration, then the same release again =="
latest="$(ls "$CHECKOUT"/backend/menu/migrations | grep -E '^[0-9]{4}_.*\.py$' | sort | tail -1)"
cat > "$CHECKOUT/backend/menu/migrations/9999_release_probe.py" <<EOF
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("menu", "${latest%.py}")]
    operations = [migrations.RunSQL("SELECT 1", migrations.RunSQL.noop)]
EOF
git -C "$CHECKOUT" add -A
git -C "$CHECKOUT" -c user.name=smoke -c user.email=smoke@example.invalid commit -qm second
revisions+=("$(git -C "$CHECKOUT" rev-parse HEAD)")
second="${revisions[1]}"
# files the second commit does not have, one per image: untracked, and git-ignored but not docker-ignored
probes=("storefront /app/release_probe.py" "storefront /app/release_probe.log"
        "operations /srv/services/order_operations/release_probe.py" "frontend /usr/share/nginx/html/release_probe.txt")
echo 'raise SystemExit("not in the commit")' > "$CHECKOUT/backend/release_probe.py"
echo probe > "$CHECKOUT/backend/release_probe.log"
echo probe > "$CHECKOUT/services/order_operations/release_probe.py"
# the commit tracks nothing under public/, yet vite copies whatever a checkout holds there into the site
mkdir -p "$CHECKOUT/frontend/public"
echo probe > "$CHECKOUT/frontend/public/release_probe.txt"
git -C "$CHECKOUT" check-ignore -q backend/release_probe.log || fail "the log probe is not git-ignored"
release second

applied="$(dc exec -T db psql -X -A -t -q -U "$SF_BOOT" -d storefront \
  -c "select count(*) from django_migrations where app = 'menu' and name = '9999_release_probe'")"
[ "$applied" = 1 ] || fail "the second release did not apply the migration of its commit"
echo "OK the second release applied the migration its commit added"

wrong=()
for svc in storefront-migrate backend relay commands-consumer operations-migrate operations-api operations-consumer \
           operations-bridge commands-relay frontend; do
  case "$svc" in
    frontend) want="asian-restaurant/frontend:$second" ;;
    operations-*|commands-relay) want="asian-restaurant/operations:$second" ;;
    *) want="asian-restaurant/storefront:$second" ;;
  esac
  id="$(dc ps -a -q "$svc")"
  [ -n "$id" ] || { wrong+=("$svc (no container)"); continue; }
  have="$(docker inspect -f '{{.Config.Image}}' "$id")"
  [ "$have" = "$want" ] || wrong+=("$svc ($have)")
done
[ "${#wrong[@]}" -eq 0 ] || fail "these do not run the second commit: ${wrong[*]}"
[ "$(docker image inspect -f '{{.Id}}' "asian-restaurant/storefront:${revisions[0]}")" != \
  "$(docker image inspect -f '{{.Id}}' "asian-restaurant/storefront:$second")" ] || fail "both commits resolved to one storefront build"
echo "OK the migration jobs and every process run the image of the second commit"

leaked=()
for probe in "${probes[@]}"; do
  read -r image path <<<"$probe"
  status=0
  docker run --rm --pull never --entrypoint sh "asian-restaurant/$image:$second" -c "test ! -e $path" || status=$?
  case "$status" in
    0) ;;
    1) leaked+=("$image:$path") ;;
    *) fail "could not look into asian-restaurant/$image:$second" ;;
  esac
done
[ "${#leaked[@]}" -eq 0 ] || fail "files the commit does not have reached its images: ${leaked[*]}"
echo "OK untracked and git-ignored files of the checkout stay out of the images"

echo "== a commit that was not built is refused =="
git -C "$CHECKOUT" -c user.name=smoke -c user.email=smoke@example.invalid commit -q --allow-empty -m third
revisions+=("$(git -C "$CHECKOUT" rev-parse HEAD)")
if out="$(cd "$CHECKOUT" && . "$WORK/dc.sh" && dc up -d 2>&1)"; then
  fail "dc() started a commit it had not built"
fi
grep -q "is not built, run dc build first" <<<"$out" || fail "the refusal does not name the missing build: $out"
if docker image inspect "asian-restaurant/storefront:${revisions[2]}" >/dev/null 2>&1; then
  fail "compose built the third commit from the checkout"
fi
echo "OK dc() refuses to start a commit whose images dc build has not made"

echo "== an uncommitted change is refused =="
echo "# uncommitted" >> "$CHECKOUT/backend/manage.py"
if out="$(cd "$CHECKOUT" && . "$WORK/dc.sh" && dc config -q 2>&1)"; then
  fail "dc() accepted a checkout with uncommitted changes"
fi
grep -q "uncommitted changes" <<<"$out" || fail "the refusal does not name the uncommitted changes: $out"
git -C "$CHECKOUT" checkout -q -- backend/manage.py
echo "OK dc() refuses a checkout whose code differs from its commit"

echo "SMOKE OK"

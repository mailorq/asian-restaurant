#!/usr/bin/env bash
# check_prod_secrets_test.sh: the production secrets check passes generated values and refuses every weak kind,
# naming the variable and never printing the value
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
tmp=$(mktemp -d)
trap 'rm -f "$tmp/base.env" "$tmp/case.env" "$tmp/dev.env"; rmdir "$tmp"' EXIT
export DEV_ENV_FILE="$tmp/dev.env"

gen() { head -c 96 /dev/urandom | base64 | tr -d '/+=\n' | head -c "$1"; }
PASSWORDS=(POSTGRES_PASSWORD STOREFRONT_DB_MIGRATOR_PASSWORD STOREFRONT_DB_RUNTIME_PASSWORD OPERATIONS_DB_PASSWORD
  OPERATIONS_DB_MIGRATOR_PASSWORD OPERATIONS_DB_RUNTIME_PASSWORD RABBITMQ_ADMIN_PASSWORD RABBITMQ_ERLANG_COOKIE
  STOREFRONT_MQ_PASSWORD OPERATIONS_MQ_PASSWORD BRIDGE_MQ_PASSWORD OPERATIONS_COMMANDS_MQ_PASSWORD)
{
  echo "DJANGO_SECRET_KEY=$(gen 64)"
  echo "OPERATIONS_SECRET_KEY=$(gen 64)"
  for name in "${PASSWORDS[@]}"; do echo "$name=$(gen 40)"; done
  echo "RABBITMQ_URL=amqp://storefront_app:$(gen 40)@rabbitmq:5672/storefront"
  echo "REDIS_URL=redis://redis:6379/1"
} > "$tmp/base.env"
echo "SOME_DEV_PASSWORD=$(gen 40)" > "$DEV_ENV_FILE"

failed=0
bash "$here/check_prod_secrets.sh" "$tmp/base.env" >/dev/null 2>&1 || { echo "FAIL generated secrets were refused"; failed=1; }

expect() {  # expect <label> <variable> <value> <reason>
  local out
  grep -v "^$2=" "$tmp/base.env" > "$tmp/case.env"
  printf '%s=%s\n' "$2" "$3" >> "$tmp/case.env"
  if out=$(bash "$here/check_prod_secrets.sh" "$tmp/case.env" 2>&1); then
    echo "FAIL $1: accepted"; failed=1; return
  fi
  local named=${2%%=*}
  if ! grep -q "refused $named.*$4" <<<"$out"; then echo "FAIL $1: expected '$named ... $4', got: $out"; failed=1; return; fi
  if [ -n "$3" ] && grep -qF -- "${3#*://*:}" <<<"$out"; then echo "FAIL $1: the value was printed"; failed=1; return; fi
  echo "ok   $1"
}

digest=$(grep -o 'sha256:[0-9a-f]\{64\}' "$repo/compose.yaml" | head -1 | cut -d: -f2)
dev_value=$(cut -d= -f2- "$DEV_ENV_FILE")
expect "empty" BRIDGE_MQ_PASSWORD "" "empty"
expect "placeholder" RABBITMQ_ADMIN_PASSWORD "change-me-$(gen 40)" "placeholder"
expect "development shape" OPERATIONS_SECRET_KEY "dev-$(gen 60)" "development"
expect "short" RABBITMQ_ERLANG_COOKIE "$(gen 20)" "shorter than 32"
expect "uniform" POSTGRES_PASSWORD "$(printf 'ab%.0s' $(seq 1 30))" "too uniform"
expect "published in the repository" STOREFRONT_MQ_PASSWORD "$digest" "published"
expect "password inside a url" RABBITMQ_URL "amqp://storefront_app:change-me-rabbit@rabbitmq:5672/storefront" "placeholder"
expect "copied from the development .env" OPERATIONS_DB_RUNTIME_PASSWORD "$dev_value" "development .env"

# the tracked e2e file used as the production one: every secret in it is refused, none is printed
if out=$(bash "$here/check_prod_secrets.sh" "$repo/.env.e2e" 2>&1); then
  echo "FAIL .env.e2e as production: accepted"; failed=1
else
  leaked=0
  while IFS='=' read -r name v; do
    [[ $name =~ (PASSWORD|SECRET_KEY)$ ]] && [ -n "$v" ] && grep -qF -- "$v" <<<"$out" && leaked=1
  done < "$repo/.env.e2e"
  if [ "$leaked" = 1 ]; then echo "FAIL .env.e2e as production: a value was printed"; failed=1
  else echo "ok   .env.e2e as production: $(grep -c '^refused' <<<"$out") refused"; fi
fi

exit "$failed"

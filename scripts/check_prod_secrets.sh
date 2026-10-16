#!/usr/bin/env bash
# check_prod_secrets.sh <env file>: refuses production secrets that are empty, shaped like a placeholder, too short
# or too uniform for a generated value, or present in a tracked file of the checked out commit (.env.e2e, examples,
# docs, test values) or in the development .env of the checkout, and refuses an environment that exports a variable
# the production render reads. it names the variable and the reason, never the value. a value that passes is not
# proven random: generate every secret, see DEPLOY.md
set -euo pipefail

env_file=${1:?usage: check_prod_secrets.sh <env file>}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
dev_env=${DEV_ENV_FILE:-$repo/.env}

# the value goes to git grep through a file of its own: a command line is readable by every user of the host
pattern=$(mktemp)
chmod 600 "$pattern"
trap 'rm -f "$pattern"' EXIT

declare -A value
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
  line=${line#export }
  name=${line%%=*}
  [[ $name =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
  v=${line#*=}
  if [[ $v =~ ^\"(.*)\"$ || $v =~ ^\'(.*)\'$ ]]; then v=${BASH_REMATCH[1]}; fi
  value[$name]=$v
done < "$env_file"

failed=0
refuse() {
  echo "refused $1: $2" >&2
  failed=$((failed + 1))
}

# bits a guesser faces if every character were drawn at random from the classes the value uses
bits() {
  local v=$1 pool=0
  [[ $v =~ [a-z] ]] && pool=$((pool + 26))
  [[ $v =~ [A-Z] ]] && pool=$((pool + 26))
  [[ $v =~ [0-9] ]] && pool=$((pool + 10))
  [[ $v =~ [^a-zA-Z0-9] ]] && pool=$((pool + 32))
  awk -v n="${#v}" -v p="$pool" 'BEGIN { printf "%d", (p > 1 ? n * log(p) / log(2) : 0) }'
}

check() {  # check <name> <value> <minimum length>
  local name=$1 v=$2 min=$3 lowered word distinct
  if [ -z "$v" ]; then refuse "$name" "empty"; return; fi
  lowered=${v,,}
  for word in change-me changeme placeholder example dummy sample default password passwd secret guest admin; do
    if [[ $lowered == *"$word"* ]]; then refuse "$name" "shaped like a placeholder"; return; fi
  done
  if [[ $lowered == dev-* || $lowered == ops-dev* || $lowered == ci-* || $lowered == test* ]]; then
    refuse "$name" "shaped like a development or test value"; return
  fi
  if [ "${#v}" -lt "$min" ]; then refuse "$name" "shorter than $min characters"; return; fi
  distinct=$(printf '%s' "$v" | fold -w1 | sort -u | wc -l)
  # 8 distinct characters: a random 32-character hex value has fewer about never, a repeated pattern does
  if [ "$distinct" -lt 8 ] || [ "$(bits "$v")" -lt 128 ]; then
    refuse "$name" "too uniform for a generated value"; return
  fi
  printf '%s\n' "$v" > "$pattern"
  if git -C "$repo" grep -q -I -F -f "$pattern" HEAD -- 2>/dev/null; then
    refuse "$name" "published: a tracked file of this commit holds it"; return
  fi
  if [ -f "$dev_env" ] && grep -q -F -f "$pattern" "$dev_env"; then
    refuse "$name" "the development .env of this checkout holds it"
  fi
}

for name in DJANGO_SECRET_KEY OPERATIONS_SECRET_KEY; do
  check "$name" "${value[$name]-}" 50
done
for name in POSTGRES_PASSWORD STOREFRONT_DB_MIGRATOR_PASSWORD STOREFRONT_DB_RUNTIME_PASSWORD \
    OPERATIONS_DB_PASSWORD OPERATIONS_DB_MIGRATOR_PASSWORD OPERATIONS_DB_RUNTIME_PASSWORD \
    RABBITMQ_ADMIN_PASSWORD RABBITMQ_ERLANG_COOKIE \
    STOREFRONT_MQ_PASSWORD OPERATIONS_MQ_PASSWORD BRIDGE_MQ_PASSWORD OPERATIONS_COMMANDS_MQ_PASSWORD; do
  check "$name" "${value[$name]-}" 32
done
# a connection url carries its password inside; one without credentials (a local redis) has nothing to check
for name in "${!value[@]}"; do
  [[ $name == *_URL ]] || continue
  if [[ ${value[$name]} =~ ^[a-z][a-z0-9+.-]*://[^/@:]*:([^/@]*)@ ]]; then
    check "$name (password)" "${BASH_REMATCH[1]}" 32
  fi
done

# compose prefers an exported variable to the same name in the file, so the values read above would not be the
# ones a container gets; the render takes every variable it reads from the file alone
if ! names=$(docker compose --env-file "$env_file" -f "$repo/compose.yaml" -f "$repo/compose.prod.yaml" \
    --profile '*' config --variables | awk 'NR > 1 { print $1 }') || [ -z "$names" ]; then
  refuse "compose" "the variables of the production render could not be listed"
else
  for name in $names; do
    # dc() sets the revision itself on the compose call
    [ "$name" = SOURCE_REVISION ] && continue
    if [ -n "${!name+x}" ]; then refuse "$name" "exported in the environment, which compose prefers to the file"; fi
  done
fi

if [ "$failed" -gt 0 ]; then
  echo "$failed production secret(s) refused, values not shown" >&2
  exit 1
fi
echo "production secrets: none empty, placeholder shaped, short, uniform, published or replaced from the environment"

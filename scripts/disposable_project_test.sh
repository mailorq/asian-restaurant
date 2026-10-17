#!/usr/bin/env bash
# disposable_project_test.sh: a smoke claims only a project name nothing carries yet, leaves a refused name untouched,
# and its teardown check names what is left. needs a docker daemon; every resource it makes is named ar_dpt_*
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/disposable_project.sh"
base="ar_dpt_$(date +%s)_$$"
subnet="10.239.$((RANDOM % 200 + 20)).0/24"
err=$(mktemp)
remove() {
  docker rm -f "${base}v_stale" >/dev/null 2>&1 || true
  docker image rm "${base}i-svc" "asian-restaurant/frontend:${base}t" "asian-restaurant/probe:${base}img" >/dev/null 2>&1 || true
  docker network rm "${base}n_default" >/dev/null 2>&1 || true
  docker volume rm "${base}v_data" >/dev/null 2>&1 || true
}
trap 'remove; rm -f "$err"' EXIT

failed=0
image() { printf 'FROM scratch\nLABEL probe=disposable_project\n' | docker build -q -t "$1" - >/dev/null; }
refused() {  # refused <label> <project>
  if (claim_project "$2") 2>"$err"; then echo "FAIL $1: claimed"; failed=1; return; fi
  if ! grep -q "refusing project $2" "$err"; then echo "FAIL $1: $(cat "$err")"; failed=1; return; fi
  echo "ok   $1"
}

if (claim_project "$base") 2>"$err"; then echo "ok   a name nothing carries is claimed"; else echo "FAIL a fresh name: $(cat "$err")"; failed=1; fi

docker volume create --label "com.docker.compose.project=${base}v" "${base}v_data" >/dev/null
refused "a project that holds a volume" "${base}v"
docker network create --label "com.docker.compose.project=${base}n" --subnet "$subnet" "${base}n_default" >/dev/null
refused "a project that holds a network" "${base}n"
image "asian-restaurant/probe:${base}img"
docker create --pull never --name "${base}v_stale" --entrypoint none "asian-restaurant/probe:${base}img" >/dev/null
docker volume rm "${base}v_data" >/dev/null
refused "a container named after the project, made outside compose" "${base}v"
image "${base}i-svc"
refused "an image compose built for the project" "${base}i"
image "asian-restaurant/frontend:${base}t"
refused "an application image tagged with the project" "${base}t"
if (claim_project "${base}") 2>"$err"; then echo "ok   projects whose names only start with a name do not block it"; else echo "FAIL prefix: $(cat "$err")"; failed=1; fi
if docker network inspect "${base}n_default" >/dev/null 2>&1; then echo "ok   a refused name keeps what it holds"; else echo "FAIL a refused name lost its network"; failed=1; fi

if teardown_clean "${base}n" 2>"$err"; then echo "FAIL a teardown that left a network: passed"; failed=1
elif grep -q "teardown of ${base}n left 1 docker resources" "$err"; then echo "ok   a teardown that left something names it"
else echo "FAIL teardown message: $(cat "$err")"; failed=1; fi
remove
if teardown_clean "${base}n" "${base}v" "${base}i" "${base}t" 2>"$err"; then echo "ok   a complete teardown passes"; else echo "FAIL a complete teardown: $(cat "$err")"; failed=1; fi

# every smoke that tears a project down claims it first and takes no project name from outside
for f in "$here"/*.sh; do
  grep -q '^trap cleanup EXIT' "$f" || continue
  name=$(basename "$f")
  claim=$(grep -n '^claim_project ' "$f" | head -1 | cut -d: -f1 || true)
  trap_at=$(grep -n '^trap cleanup EXIT' "$f" | head -1 | cut -d: -f1)
  if [ -z "$claim" ] || [ "$claim" -gt "$trap_at" ]; then echo "FAIL $name arms its teardown before it claims the project"; failed=1; continue; fi
  if grep -qE '^(PROJ|SOURCE|TARGET)="\$\{' "$f"; then echo "FAIL $name takes its project name from the environment"; failed=1; continue; fi
  if ! grep -q 'teardown_clean ' "$f"; then echo "FAIL $name does not check what its teardown left"; failed=1; continue; fi
  echo "ok   $name claims its project and checks its teardown"
done

exit "$failed"

# sourced by the smokes. their teardown runs down -v on a project name, so a name is claimed only while nothing
# carries it yet, and the teardown counts what is left instead of trusting each step

project_resources() {  # project_resources <project>: how many containers, volumes, networks and images carry the name
  local p=$1 found
  found=$(
    docker ps -aq --filter "label=com.docker.compose.project=$p" &&
      docker ps -aq --filter "name=^${p}[-_]" &&
      docker volume ls -q --filter "label=com.docker.compose.project=$p" &&
      docker network ls -q --filter "label=com.docker.compose.project=$p" &&
      docker images -q --filter "reference=${p}-*" &&
      docker images -q --filter "reference=asian-restaurant/*:$p"
  ) || return 1
  printf '%s\n' "$found" | sort -u | grep -c . || true
}

claim_project() {  # claim_project <project>...: refuses a name that already carries resources, before any teardown is armed
  local p n
  for p in "$@"; do
    if ! n=$(project_resources "$p"); then
      echo "refusing $p: docker could not be asked what it holds" >&2
      exit 2
    fi
    if [ "$n" -ne 0 ]; then
      echo "refusing $p: $n docker resources already carry this name and the teardown would remove them" >&2
      exit 2
    fi
  done
}

teardown_clean() {  # teardown_clean <project>...: true when nothing carries the names, otherwise names what is left
  local p n clean=0
  for p in "$@"; do
    if ! n=$(project_resources "$p"); then
      echo "teardown of $p could not be checked: docker did not answer" >&2
      clean=1
    elif [ "$n" -ne 0 ]; then
      echo "teardown of $p left $n docker resources: containers, volumes, networks or images named after it" >&2
      clean=1
    fi
  done
  return "$clean"
}

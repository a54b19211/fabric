#!/usr/bin/env bash
# The integration suite (tests/integration_*.py) on a throwaway PostgreSQL (paradedb) container.
#
#   bash ops/run_integration_tests.sh [tests/integration_x.py ...]     (`make test-integration` runs all)
#
# The fixtures only accept a fixed loopback port (18436), which may be taken on the
# host. Those groups run inside a private network namespace (unshare -rn) in which small forwarders on
# 127.0.0.1 reach the containers through their unix sockets; on the host the containers listen on 127.0.0.1
# above 19800 only.
#
# Containers: each one is started with the label fabric-itest.run=<this run's random id>, and only the IDs
# docker returned for this run (or, when `docker run` created a container but could not start it, the ID of
# the container with this run's label and exact name) are registered, with their image and anonymous
# volumes, as JSON lines in the manifest. The teardown - on success, on failure and on interruption - checks
# each registered ID still carries this run's label, stops it, removes it with its anonymous volumes
# (`docker rm -v`; nothing is scanned or pruned) and records what it removed and whether anything is left.
# The manifest is kept. A container this run did not create is never stopped or removed.
#
# PYTHON           the interpreter with the locked dependencies (default .venv/bin/python, `make venv`)
# ITEST_PREFIX     container name prefix (default fabric-itest)
# ITEST_MANIFEST   the container manifest (default .test-artifacts/fabric-itest-<run id>.containers.jsonl in the checkout, git-ignored)
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
PREFIX=${ITEST_PREFIX:-fabric-itest}
PG_IMAGE=paradedb/paradedb:v0.25.11-pg17
[ -x "$PY" ] || { echo "no interpreter at $PY: run make venv first" >&2; exit 2; }

R=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
PW=$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')
PG=$PREFIX-pg-$R
LABEL=fabric-itest.run
MANIFEST=${ITEST_MANIFEST:-$ROOT/.test-artifacts/fabric-itest-$R.containers.jsonl}
mkdir -p "$(dirname "$MANIFEST")"
WORK=$(mktemp -d /tmp/fabric-itest.XXXXXX)
SOCK=$WORK/pgsock

# ---- containers: register only what this run created; tear down only that ----
IDS=""  # registered container IDs, newest first (torn down in that order)
declare -A VOLUMES=()
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
record() { printf '{"at":"%s",%s}\n' "$(now)" "$1" >> "$MANIFEST"; }
json_list() { local out="" v; for v in "$@"; do out="$out${out:+,}\"$v\""; done; printf '[%s]' "$out"; }

register() {  # role name id
  local vols image
  IDS="$3 $IDS"
  vols=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$3" 2>/dev/null)
  image=$(docker inspect -f '{{.Config.Image}} {{.Image}}' "$3" 2>/dev/null)
  VOLUMES[$3]=$vols
  # shellcheck disable=SC2086
  record "\"event\":\"created\",\"role\":\"$1\",\"name\":\"$2\",\"id\":\"$3\",\"image\":\"${image% *}\",\"image_id\":\"${image#* }\",\"anonymous_volumes\":$(json_list $vols)"
}

start() {  # role name [docker run options...] image [command...]
  local role=$1 name=$2 id rc
  shift 2
  id=$(docker run -d --name "$name" --label "$LABEL=$R" "$@")
  rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$id" ]; then
    register "$role" "$name" "$id"
    return 0
  fi
  # `docker run` can create the container and then fail to start it (e.g. a port taken meanwhile)
  id=$(docker ps -aq --no-trunc --filter "label=$LABEL=$R" --filter "name=^/$name\$" 2>/dev/null)
  record "\"event\":\"start_failed\",\"role\":\"$role\",\"name\":\"$name\",\"exit\":$rc,\"created_id\":\"$id\""
  [ -n "$id" ] && register "$role" "$name" "$id"
  return 1
}

teardown() {
  local rc=$1 id v label left="" vleft=""
  for id in $IDS; do
    label=$(docker inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$id" 2>/dev/null)
    if [ -z "$label" ] && ! docker inspect "$id" >/dev/null 2>&1; then
      record "\"event\":\"already_gone\",\"id\":\"$id\""
    elif [ "$label" != "$R" ]; then
      record "\"event\":\"not_touched\",\"id\":\"$id\",\"reason\":\"label is not this run's\""
      continue
    else
      docker stop -t 20 "$id" >/dev/null 2>&1
      docker rm -v "$id" >/dev/null 2>&1
    fi
    docker inspect "$id" >/dev/null 2>&1 && left="$left $id"
    for v in ${VOLUMES[$id]}; do
      # the container's own anonymous volume, recorded at creation; gone with `rm -v` unless removed apart
      docker volume inspect "$v" >/dev/null 2>&1 && docker volume rm "$v" >/dev/null 2>&1
      docker volume inspect "$v" >/dev/null 2>&1 && vleft="$vleft $v"
    done
    # shellcheck disable=SC2086
    record "\"event\":\"removed\",\"id\":\"$id\",\"container_left\":$(docker inspect "$id" >/dev/null 2>&1 && echo true || echo false),\"volumes\":$(json_list ${VOLUMES[$id]})"
  done
  rm -rf "$WORK" 2>/dev/null
  # shellcheck disable=SC2086
  record "\"event\":\"teardown\",\"exit\":$rc,\"containers_left\":$(json_list $left),\"volumes_left\":$(json_list $vleft)"
  echo "containers of this run torn down (manifest $MANIFEST)" >&2
  if [ -n "$left$vleft" ]; then
    echo "left behind:$left$vleft" >&2
    [ "$rc" -eq 0 ] && rc=3
  fi
  exit "$rc"
}
trap 'teardown $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
record "\"event\":\"run\",\"run\":\"$R\",\"prefix\":\"$PREFIX\",\"label\":\"$LABEL=$R\""

free_port() { for p in $(seq "$1" 19990); do ss -ltn | grep -q ":$p " || { echo "$p"; return; }; done; }
P1=$(free_port 19871)
mkdir -p "$SOCK"

start pg "$PG" -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=zk_fabric_test \
  -v "$SOCK":/var/run/postgresql -p "127.0.0.1:$P1:5432" "$PG_IMAGE" >/dev/null || exit 2
echo "container: $PG (127.0.0.1:$P1); manifest $MANIFEST"
for _ in $(seq 60); do docker exec "$PG" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 && break; sleep 1; done
sleep 3  # the image restarts PostgreSQL once after its init scripts
for _ in $(seq 30); do
  docker exec "$PG" psql -U postgres -d zk_fabric_test -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null 2>&1 && break
  sleep 1
done
if [ $# -gt 0 ]; then TARGETS="$*"; else TARGETS=$(ls tests/integration_*.py | tr '\n' ' '); fi
export PYTHONDONTWRITEBYTECODE=1
OPTS="-p no:cacheprovider -o addopts= -q -rfEs"
FWD="$PY $ROOT/ops/unix_forward.py"
FAILED=""

# one private namespace per group: forwarders for the fixed ports, then pytest; the group's exit code is kept
in_ns() {
  timeout 2400 unshare -rn bash -c "ip link set lo up
    for p in 18436; do $FWD $SOCK/.s.PGSQL.5432 \$p >/dev/null 2>&1 & done
    sleep 1; $PY -m pytest $OPTS $1; RC=\$?; kill \$(jobs -p); exit \$RC"
}
group() {  # name, then the command; records a failing group
  local name=$1
  shift
  echo "== $name"
  "$@" || FAILED="$FAILED $name"
}
DATAFABRIC_DATABASE_URL=postgresql://postgres:$PW@127.0.0.1:18436/zk_fabric_test \
  group "netns group:$TARGETS" in_ns "$TARGETS"
if [ -n "$FAILED" ]; then
  echo "integration groups that failed:$FAILED" >&2
  exit 1
fi
echo "integration suite ok"

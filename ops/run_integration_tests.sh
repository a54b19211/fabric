#!/usr/bin/env bash
# The integration suite (tests/integration_*.py) on throwaway PostgreSQL (paradedb) and Redis containers.
#
#   bash ops/run_integration_tests.sh [tests/integration_x.py ...]     (`make test-integration` runs all)
#
# Several fixtures only accept fixed loopback ports (18436, 18636, 18736, 18737), which may be taken on the
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
# BIOCHECK_SRC     a BioCheck checkout for the real-checker tests (default ../clean-biocheck; without one
#                  those tests are reported as skipped)
# BIOCHECK_PYTHON  the interpreter of that checkout (default $BIOCHECK_SRC/.venv/bin/python)
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
PREFIX=${ITEST_PREFIX:-fabric-itest}
BIOCHECK_SRC=${BIOCHECK_SRC:-$ROOT/../clean-biocheck}
BIOCHECK_PYTHON=${BIOCHECK_PYTHON:-$BIOCHECK_SRC/.venv/bin/python}
PG_IMAGE=paradedb/paradedb:v0.25.11-pg17
REDIS_IMAGE=redis:7.4-alpine
[ -x "$PY" ] || { echo "no interpreter at $PY: run make venv first" >&2; exit 2; }

R=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
PW=$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')
PG=$PREFIX-pg-$R RTPG=$PREFIX-pg-rt-$R RD=$PREFIX-redis-$R RDS=$PREFIX-redis-sock-$R
LABEL=fabric-itest.run
MANIFEST=${ITEST_MANIFEST:-$ROOT/.test-artifacts/fabric-itest-$R.containers.jsonl}
mkdir -p "$(dirname "$MANIFEST")"
WORK=$(mktemp -d /tmp/fabric-itest.XXXXXX)
SOCK=$WORK/pgsock RSOCK=$WORK/redissock RUN=$WORK/run

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
P1=$(free_port 19871); P2=$(free_port $((P1 + 1))); P3=$(free_port $((P2 + 1))); P4=$(free_port $((P3 + 1)))
mkdir -p "$SOCK" "$RSOCK" "$RUN"
chmod 777 "$RSOCK"  # the redis user of the container writes its socket here

start pg "$PG" -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=fabric_history \
  -v "$SOCK":/var/run/postgresql -p "127.0.0.1:$P1:5432" "$PG_IMAGE" >/dev/null || exit 2
start pg-runtime "$RTPG" -e POSTGRES_USER=datafabric_runtime_test -e POSTGRES_DB=datafabric_runtime_test \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p "127.0.0.1:$P2:5432" "$PG_IMAGE" >/dev/null || exit 2
start redis "$RD" -p "127.0.0.1:$P3:6379" "$REDIS_IMAGE" >/dev/null || exit 2
start redis-sock "$RDS" -v "$RSOCK":/sock -p "127.0.0.1:$P4:6379" "$REDIS_IMAGE" \
  redis-server --unixsocket /sock/redis.sock --unixsocketperm 777 >/dev/null || exit 2
echo "containers: $PG (127.0.0.1:$P1), $RTPG ($P2), $RD ($P3), $RDS ($P4); manifest $MANIFEST"
for c in "$PG" "$RTPG"; do
  for _ in $(seq 60); do docker exec "$c" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 && break; sleep 1; done
done
sleep 3  # the image restarts PostgreSQL once after its init scripts
for db in fabric_history bioradar_kdd_opus_fabric bioradar_kdd_opus_history fabric_demo_test; do
  [ "$db" = fabric_history ] || docker exec "$PG" psql -U postgres -c "CREATE DATABASE $db" >/dev/null
  for _ in $(seq 30); do
    docker exec "$PG" psql -U postgres -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null 2>&1 && break
    sleep 1
  done
done
# the runtime database starts with only what the migrations expect (plpgsql, vector)
for e in $(docker exec "$RTPG" psql -U datafabric_runtime_test -d datafabric_runtime_test -At \
  -c "SELECT extname FROM pg_extension WHERE extname NOT IN ('plpgsql','vector')"); do
  docker exec "$RTPG" psql -U datafabric_runtime_test -d datafabric_runtime_test \
    -c "DROP EXTENSION IF EXISTS \"$e\" CASCADE" >/dev/null 2>&1
done
docker exec "$RTPG" psql -U datafabric_runtime_test -d datafabric_runtime_test \
  -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null 2>&1

if [ $# -gt 0 ]; then TARGETS="$*"; else TARGETS=$(ls tests/integration_*.py | tr '\n' ' '); fi
NS="" RT="" ID="" DEMO=""
for t in $TARGETS; do
  case "$t" in
    *integration_extraction_runtime*|*integration_research_read*) RT="$RT $t" ;;
    *integration_identity_review*) ID="$ID $t" ;;
    *integration_demo_fetch*) DEMO="$DEMO $t" ;;
    *) NS="$NS $t" ;;
  esac
done
export PYTHONDONTWRITEBYTECODE=1
if [ -f "$BIOCHECK_SRC/app/service.py" ]; then
  export DATAFABRIC_TEST_BIOCHECK_SRC=$BIOCHECK_SRC DATAFABRIC_TEST_BIOCHECK_PYTHON=$BIOCHECK_PYTHON
  echo "BioCheck checkout for the real-checker tests: $BIOCHECK_SRC"
else
  echo "no BioCheck checkout at $BIOCHECK_SRC: the real-checker tests are skipped"
fi
OPTS="-p no:cacheprovider -o addopts= -q -rfEs"
FWD="$PY $ROOT/ops/unix_forward.py"
FAILED=""

# one private namespace per group: forwarders for the fixed ports, then pytest; the group's exit code is kept
in_ns() {
  timeout 2400 unshare -rn bash -c "ip link set lo up
    for p in 18436 18636 18736; do $FWD $SOCK/.s.PGSQL.5432 \$p >/dev/null 2>&1 & done
    $FWD $RSOCK/redis.sock 18737 >/dev/null 2>&1 &
    sleep 1; ${PRE:-true}; $PY -m pytest $OPTS $1; RC=\$?; kill \$(jobs -p); exit \$RC"
}
group() {  # name, then the command; records a failing group
  local name=$1
  shift
  echo "== $name"
  "$@" || FAILED="$FAILED $name"
}
if [ -n "$NS" ]; then
  DATAFABRIC_HISTORY_DATABASE_URL=postgresql://postgres:$PW@127.0.0.1:18436/fabric_history \
    group "netns group:$NS" in_ns "$NS"
fi
if [ -n "$ID" ]; then
  DATAFABRIC_PUBLICATION_DATABASE_URL=postgresql://postgres:$PW@127.0.0.1:18636/bioradar_kdd_opus_fabric \
  DATAFABRIC_HISTORY_DATABASE_URL=postgresql://postgres:$PW@127.0.0.1:18636/bioradar_kdd_opus_history \
    group "identity group:$ID" in_ns "$ID"
fi
if [ -n "$DEMO" ]; then
  INSTANCE=$PREFIX-demo-$R
  cat > "$RUN/receipt.json" <<JSON
{"created_by_this_run": true, "existing_resources_used": false, "run_id": "$INSTANCE",
 "purpose": "isolated Fabric demo fetch contract regression; no real model/public crawl",
 "database_name": "fabric_demo_test",
 "resources": [{"role": "postgres", "host": "127.0.0.1", "port": 18736, "container": "$PG"},
               {"role": "redis", "host": "127.0.0.1", "port": 18737, "container": "$RDS"}]}
JSON
  DATAFABRIC_DEMO_TEST_RESOURCE_RECEIPT=$RUN/receipt.json \
  DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG="{\"instance_id\": \"$INSTANCE\", \"database\": {\"host\": \"127.0.0.1\", \"port\": 18736, \"name\": \"fabric_demo_test\"}, \"redis\": {\"host\": \"127.0.0.1\", \"port\": 18737, \"db\": 0}, \"input_file\": \"$RUN/input.json\"}" \
  DATAFABRIC_HISTORY_DATABASE_URL=postgresql://postgres:$PW@127.0.0.1:18736/fabric_demo_test \
  DATAFABRIC_HISTORY_REDIS_URL=redis://127.0.0.1:18737/0 \
  DATAFABRIC_HISTORY_ADMIN_TOKEN=synthetic-admin-$R DATAFABRIC_HISTORY_READ_TOKEN=synthetic-read-$R \
  DATAFABRIC_DEMO_RUNTIME_OUTPUT_DIR=$RUN PRE="$PY -m migration.history" \
    group "demo group:$DEMO" in_ns "$DEMO"
fi
if [ -n "$RT" ]; then
  DATAFABRIC_TEST_DB_PORT=$P2 DATAFABRIC_TEST_REDIS_PORT=$P3 \
  DATAFABRIC_RESEARCH_TEST_DATABASE_URL=postgresql://datafabric_runtime_test@127.0.0.1:$P2/datafabric_runtime_test \
    group "runtime group:$RT" timeout 900 "$PY" -m pytest $OPTS $RT
fi
if [ -n "$FAILED" ]; then
  echo "integration groups that failed:$FAILED" >&2
  exit 1
fi
echo "integration suite ok"

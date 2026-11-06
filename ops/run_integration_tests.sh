#!/usr/bin/env bash
# The integration suite (tests/integration_*.py) on throwaway PostgreSQL (paradedb) and Redis containers.
#
#   bash ops/run_integration_tests.sh [tests/integration_x.py ...]     (`make test-integration` runs all)
#
# Several fixtures only accept fixed loopback ports (18436, 18636, 18736, 18737), which may be taken on the
# host. Those groups run inside a private network namespace (unshare -rn) in which small forwarders on
# 127.0.0.1 reach the containers through their unix sockets; on the host the containers listen on 127.0.0.1
# above 19800 only. Every container this script starts is named <prefix>-<role>-<random> and removed by that
# exact name on exit, also when a step fails. Nothing else is touched.
#
# PYTHON           the interpreter with the locked dependencies (default .venv/bin/python, `make venv`)
# ITEST_PREFIX     container name prefix (default fabric-itest)
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
WORK=$(mktemp -d /tmp/fabric-itest.XXXXXX)
SOCK=$WORK/pgsock RSOCK=$WORK/redissock RUN=$WORK/run
cleanup() {
  docker stop -t 20 "$PG" "$RDS" >/dev/null 2>&1
  docker rm -f "$PG" "$RTPG" "$RD" "$RDS" >/dev/null 2>&1
  rm -rf "$WORK" 2>/dev/null
}
trap cleanup EXIT
free_port() { for p in $(seq "$1" 19990); do ss -ltn | grep -q ":$p " || { echo "$p"; return; }; done; }
P1=$(free_port 19871); P2=$(free_port $((P1 + 1))); P3=$(free_port $((P2 + 1))); P4=$(free_port $((P3 + 1)))
mkdir -p "$SOCK" "$RSOCK" "$RUN"
chmod 777 "$RSOCK"  # the redis user of the container writes its socket here

docker run -d --name "$PG" -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=fabric_history \
  -v "$SOCK":/var/run/postgresql -p "127.0.0.1:$P1:5432" "$PG_IMAGE" >/dev/null || exit 2
docker run -d --name "$RTPG" -e POSTGRES_USER=datafabric_runtime_test -e POSTGRES_DB=datafabric_runtime_test \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p "127.0.0.1:$P2:5432" "$PG_IMAGE" >/dev/null || exit 2
docker run -d --name "$RD" -p "127.0.0.1:$P3:6379" "$REDIS_IMAGE" >/dev/null || exit 2
docker run -d --name "$RDS" -v "$RSOCK":/sock -p "127.0.0.1:$P4:6379" "$REDIS_IMAGE" \
  redis-server --unixsocket /sock/redis.sock --unixsocketperm 777 >/dev/null || exit 2
echo "containers: $PG (127.0.0.1:$P1), $RTPG ($P2), $RD ($P3), $RDS ($P4)"
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

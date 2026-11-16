#!/usr/bin/env bash
# Build a fresh virtual environment from requirements.lock (every distribution pinned by sha256) and check
# that every module imports. `make check-install` runs it.
#   bash ops/check_clean_install.sh
# UV: the uv binary (default uv on PATH). PYTHON: interpreter for the environment (default 3.10, the lock is
# resolved for it). UV_INDEX_URL may point at a mirror. The environment is removed afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
UV=${UV:-uv}
ENV_DIR=$(mktemp -d)
trap 'rm -rf "$ENV_DIR"' EXIT
"$UV" venv --quiet --python "${PYTHON:-3.10}" "$ENV_DIR"
"$UV" pip sync --quiet --python "$ENV_DIR/bin/python" --require-hashes requirements.lock
"$ENV_DIR/bin/python" ops/check_imports.py

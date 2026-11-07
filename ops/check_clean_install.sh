#!/usr/bin/env bash
# Build a fresh virtual environment from requirements.lock (every distribution, the extraction-contract wheel
# included, pinned by sha256) and check that every module imports. `make check-install` runs it.
#   bash ops/check_clean_install.sh
# UV: the uv binary (default uv on PATH). PYTHON: interpreter for the environment (default 3.10, the lock is
# resolved for it). EXTRACTION_CONTRACT_WHEELS: directory holding the extraction-contract wheel (default
# ../extraction-contract/dist). LLM_TRANSPORT_WHEELS: directory holding the zk-llm-transport wheel (default
# ../llm-transport/dist). UV_INDEX_URL may point at a mirror. The environment is removed afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
UV=${UV:-uv}
ENV_DIR=$(mktemp -d)
trap 'rm -rf "$ENV_DIR"' EXIT
"$UV" venv --quiet --python "${PYTHON:-3.10}" "$ENV_DIR"
"$UV" pip sync --quiet --python "$ENV_DIR/bin/python" --require-hashes \
  --find-links "${EXTRACTION_CONTRACT_WHEELS:-../extraction-contract/dist}" \
  --find-links "${LLM_TRANSPORT_WHEELS:-../llm-transport/dist}" requirements.lock
"$ENV_DIR/bin/python" ops/check_imports.py

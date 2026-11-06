#!/usr/bin/env bash
# Build a fresh virtual environment from the pinned requirements and check that every module imports.
#   bash ops/check_clean_install.sh [python]
# EXTRACTION_CONTRACT_WHEELS: directory holding the extraction-contract wheel (default ../extraction-contract/dist).
# PIP_INDEX_URL may point at a mirror. The environment is removed afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=${1:-python3}
ENV_DIR=$(mktemp -d)
trap 'rm -rf "$ENV_DIR"' EXIT
"$PYTHON" -m venv "$ENV_DIR"
"$ENV_DIR/bin/pip" install --quiet -r requirements.txt
"$ENV_DIR/bin/pip" install --quiet --no-index --find-links "${EXTRACTION_CONTRACT_WHEELS:-../extraction-contract/dist}" \
  --require-hashes -r requirements-contract.txt
"$ENV_DIR/bin/python" ops/check_imports.py

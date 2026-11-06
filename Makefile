# The one entry point for this checkout (README: 开发环境与测试).
# uv 0.13.0 from PATH (~/.local/bin/uv) or UV=<path>. requirements.lock is resolved for CPython 3.10 (the
# interpreter of the running Fabric) on x86_64-unknown-linux-gnu, with a sha256 for every distribution;
# `make venv` installs only from it.
UV ?= uv
PYTHON ?= 3.10
# the shared contract wheel (built in the sibling extraction-contract checkout; hash in the lock)
EXTRACTION_CONTRACT_WHEELS ?= ../extraction-contract/dist
# the real-checker integration tests run the BioCheck rules from a BioCheck checkout in its own venv
BIOCHECK_SRC ?= ../clean-biocheck
BIOCHECK_PYTHON ?= $(BIOCHECK_SRC)/.venv/bin/python
VENV ?= .venv
PY = $(VENV)/bin/python
LOCK_INPUTS = requirements.txt requirements-contract.txt
LOCK_FLAGS = --generate-hashes --python-version 3.10 --python-platform x86_64-unknown-linux-gnu \
	--find-links $(EXTRACTION_CONTRACT_WHEELS)

.PHONY: help venv test test-integration check check-install check-lock lock
help:
	@echo 'make venv              $(VENV) from requirements.lock only (hash-checked, Python $(PYTHON))'
	@echo 'make test              the unit suite (tests/test_*.py, no database)'
	@echo 'make test-integration  tests/integration_*.py on throwaway PostgreSQL/Redis containers'
	@echo 'make check             test + test-integration + check-lock'
	@echo 'make check-install     a throwaway venv from the lock; import every module'
	@echo 'make check-lock        requirements.lock still matches $(LOCK_INPUTS) (reads the index)'
	@echo 'make lock              re-resolve requirements.lock after a change of $(LOCK_INPUTS)'
venv:
	test -x $(PY) || $(UV) venv --quiet --python $(PYTHON) $(VENV)
	$(UV) pip sync --quiet --python $(PY) --require-hashes --find-links $(EXTRACTION_CONTRACT_WHEELS) \
	  requirements.lock
# pytest.ini already passes -q
test: venv
	$(PY) -m pytest
test-integration: venv
	PYTHON=$(abspath $(PY)) BIOCHECK_SRC=$(abspath $(BIOCHECK_SRC)) BIOCHECK_PYTHON=$(abspath $(BIOCHECK_PYTHON)) \
	  bash ops/run_integration_tests.sh
check: test test-integration check-lock
check-install:
	UV=$(UV) PYTHON=$(PYTHON) EXTRACTION_CONTRACT_WHEELS=$(EXTRACTION_CONTRACT_WHEELS) bash ops/check_clean_install.sh
# the lock is re-resolved with itself as the preference: unchanged inputs give the same pins
check-lock:
	@tmp=$$(mktemp -d) && trap 'rm -rf $$tmp' EXIT && cp requirements.lock $$tmp/lock && \
	$(UV) pip compile --quiet $(LOCK_INPUTS) $(LOCK_FLAGS) -o $$tmp/lock && \
	grep -v '^#' requirements.lock > $$tmp/a && grep -v '^#' $$tmp/lock > $$tmp/b && \
	{ cmp -s $$tmp/a $$tmp/b && echo 'requirements.lock is current'; } || \
	{ diff $$tmp/a $$tmp/b; echo 'requirements.lock is stale: run make lock'; exit 1; }
lock:
	$(UV) pip compile $(LOCK_INPUTS) $(LOCK_FLAGS) -o requirements.lock

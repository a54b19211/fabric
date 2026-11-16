"""Import every module of the service packages; exit 1 if any import fails.

Run with the interpreter of the environment under test (ops/check_clean_install.sh builds a fresh one):
    python ops/check_imports.py
Importing must not need a database, Redis or network: modules connect lazily.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("api", "clean", "fetch", "run", "store")


def modules():
    for package in PACKAGES:
        yield package
        for info in pkgutil.walk_packages([str(ROOT / package)], prefix=package + "."):
            yield info.name


def main() -> int:
    sys.path.insert(0, str(ROOT))
    failures = []
    count = 0
    for name in modules():
        count += 1
        try:
            importlib.import_module(name)
        except (Exception, SystemExit):  # noqa: BLE001 - every failure is reported, none stops the scan
            failures.append((name, traceback.format_exc(limit=1).strip().splitlines()[-1]))
    for name, error in failures:
        print(f"FAIL {name}: {error}")
    print(f"{count - len(failures)}/{count} modules import")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

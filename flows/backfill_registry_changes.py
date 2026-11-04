"""给已采用的临床登记版本补登记差分（V022）。新版本在发布事务里已经同时差分，这里只补存量。

用法：python -m flows.backfill_registry_changes [source_id]
可重复执行；已有差分的版本跳过。
"""

import json
import sys

from db.registry_changes import backfill


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else "clinicaltrials"
    print(json.dumps({"source_id": source, **backfill(source)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

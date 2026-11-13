"""核验程序（常驻）：把清洗段和测量程序登记的核验任务交给 BioCheck，核对 Fabric 抽出来的编号、画像和读数与原文对不对得上，
结果写进 derived_extraction_checks（测量读数还会整理成测量对象 derived_measurements）。真正的工作在 clean/extraction_checks.py。
没配置 BioCheck（DATAFABRIC_BIOCHECK_URL、DATAFABRIC_BIOCHECK_TOKEN）时打印 disabled 就退出。

用法：python -m run.checks        （线上是 ops/systemd/datafabric-checks.service）
"""

import json
import signal
import threading

from clean.extraction_checks import configured, run_worker


def main():
    if not configured():
        print(json.dumps({"extraction_checks": {"state": "disabled"}}))
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_worker(stop=stop)


if __name__ == "__main__":
    main()

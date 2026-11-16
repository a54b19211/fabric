"""测量程序：从清洗正文里读出数字，直接写成测量对象（核验标记一律“未核”，照样往下流）。
两条路线读同一批窗口：模型路线（设了 DATAFABRIC_MEASURE_LLM_BASE_URL 和 DATAFABRIC_MEASURE_LLM_MODEL 才有）和
规则路线（WHO DON 的明确计数规则，不调模型；DATAFABRIC_MEASURE_RULES_ROUTE=false 关掉）。两条都没有时什么也不做。

从哪读起：run_round()：先给新的清洗正文排运行（clean/measure_runs.py 的 plan），再一个窗口一个窗口地读
（process_once），直到没有到期的窗口或读满 max_windows 个。run_worker() 一轮接一轮，main() 是命令行。
怎么读数字在 clean/measure_extract.py，接口怎么读对象在 clean/measurement_reads.py。

    python -m run.measure     # 一轮接一轮，直到收到 SIGTERM；没有任何路线时只打印 disabled
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time

from clean import measure_extract, measure_runs

ROUND_WINDOWS = 200  # windows one round reads at most


def rules_from_env() -> bool:
    return os.environ.get("DATAFABRIC_MEASURE_RULES_ROUTE", "true").lower() != "false"


def run_round(*, model=None, with_rules=None, max_windows=ROUND_WINDOWS) -> dict:
    """One round: plan the new clean texts, then read due windows until none is left (or max_windows)."""
    model = model or measure_extract.model_from_env()
    with_rules = rules_from_env() if with_rules is None else with_rules
    if model is None and not with_rules:
        return {"state": "disabled"}
    counts = {"planned": measure_runs.plan(model=model, with_rules=with_rules), "windows": {}}
    for _ in range(max_windows):
        state = measure_runs.process_once(model=model, with_rules=with_rules)["state"]
        counts["windows"][state] = counts["windows"].get(state, 0) + 1
        if state == "idle":
            break
    return counts


def run_worker(*, stop, idle_seconds=30.0, rounds=None):
    n = 0
    while not stop.is_set() and (rounds is None or n < rounds):
        n += 1
        counts = run_round()
        if counts.get("state") == "disabled" or counts["windows"].get("idle"):
            stop.wait(idle_seconds)


def main():
    if measure_extract.model_from_env() is None and not rules_from_env():
        print(json.dumps({"measure_extract": {"state": "disabled"}}))
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    started = time.monotonic()
    run_worker(stop=stop)
    print(json.dumps({"measure_extract": {"state": "stopped", "seconds": round(time.monotonic() - started)}}))


if __name__ == "__main__":
    main()

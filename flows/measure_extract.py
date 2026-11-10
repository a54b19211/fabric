"""测量抽取程序：从清洗正文里读出数字读数（样本量、百分比、人数等），连同它在正文里的位置存起来。
两条路线读同一批窗口（正文切成的小段）：模型路线（设了 DATAFABRIC_MEASURE_LLM_* 才有）和规则路线
（who.explicit-count.v1 的明确计数规则，不调模型、不发请求；DATAFABRIC_MEASURE_RULES_ROUTE=false 关掉）。两条都没有时什么也不做。

从哪读起：run_round()，它是一轮的全部步骤：给每份新的清洗正文排一个抽取运行（run）；在本轮模型请求预算内读到期的窗口
（预算用完本轮就结束，没读的窗口标 budget_spent，留给下一轮）；把来源恢复可读后的运行接着读完；再跟进各来源声明的更正。
run_worker() 是不停转轮的循环，run_scope() 只测一份清单，main() 是命令行。每一步的实现在 db/measure_runs.py（plan、
process_once、settle）和 db/measurements.py（scan_corrections）。

读写的表：读 derived_clean_text（清洗正文）、history_versions、history_sources；写 derived_measure_runs、derived_measure_windows、
derived_measure_window_attempts、derived_measure_requests、derived_measure_readings（运行、窗口、每次尝试、模型请求、读到的读数），
以及 derived_measure_relocations（清洗器升级后，旧读数在新正文里重新定位）；跟进更正时写 derived_measure_correction_cursor、
_claims、_links、_resolutions，并给已有的测量对象加事件（derived_measurement_events）。读数变成对象（derived_measurements）、
交核验服务核对，是 db/extraction_checks.py 那个程序的事，不在这里。

原有说明里的事实：

常驻队列会给每个可测来源的每份当前清洗正文排运行，但有界回填（flows/who_backfill）新建的版本除外：背景材料只有在清单里点名时才测。

    python -m flows.measure_extract                      # 一轮接一轮，直到收到 SIGTERM；没有任何路线时只打印 disabled
    python -m flows.measure_extract --scope list.json    # 只测一份明确的清单，测一次
    python -m flows.measure_extract --scope list.json --upgrade    # 同上，也重测旧 producer 读过的版本

清单的格式是 {"versions": ["<版本号>", {"version_id": "<版本号>", "windows": [0, 3]}]}：只写版本号就读它的所有窗口，
带 "windows" 的只读这些窗口（其余窗口标为 out_of_scope，不算欠账）。排计划和读都会把清单的摘要写在每个运行上；
打印的回执写明计划了什么、哪些版本已经有运行、哪些没排成以及原因。一次调用最多读 MAX_SCOPE_ROUNDS 轮；
还在退避中或供应商预算用完的窗口，留给下一次调用。

同一份正文、同一条路线已经有运行的版本不会再排（清单不会让已有的运行变大）。加 --upgrade 时只认当前 producer
（处理器版本和模型配置）的运行：旧 producer 读过的版本会另起一个新运行，旧运行原样保留。
"""

from __future__ import annotations

import argparse
import json
import os
import time

from sqlalchemy import text

from db import history, measure_runs, measurements

ROUND_REQUESTS = int(os.environ.get("DATAFABRIC_MEASURE_ROUND_REQUESTS", "50"))
ROUND_WINDOWS = 200
MAX_SCOPE_ROUNDS = 20


def _sources(conn):
    return [r[0] for r in conn.execute(text("SELECT source_id FROM history_sources WHERE read_enabled ORDER BY source_id"))
            if measure_runs._readable(conn, r[0])]


def _plan(model, with_rules, scope, producer_upgrade):
    """(runs planned, the scope's receipt or None): the standing queue, or an explicit list only."""
    if scope is None:
        return measure_runs.plan(model=model, with_rules=with_rules), None
    receipt = measure_runs.plan_scope(scope, model=model, with_rules=with_rules, producer_upgrade=producer_upgrade)
    return sum(len(route["planned"]) for route in receipt["routes"].values()), receipt


def run_round(*, budget_requests=ROUND_REQUESTS, max_windows=ROUND_WINDOWS, model=None, client=None, scope=None,
              with_rules=None, producer_upgrade=False) -> dict:
    """One round; its counts include the model requests it sent (usage) and, for a scope, the plan's receipt
    (producer_upgrade: see plan_scope; it has no meaning without a scope)."""
    model = model or measure_runs.model_from_env()
    with_rules = measure_runs.rules_from_env() if with_rules is None else with_rules
    if model is None and not with_rules:
        return {"state": "disabled"}
    planned, receipt = _plan(model, with_rules, scope, producer_upgrade)
    counts = {"planned": planned, "resumed": measure_runs.resume(), "windows": {}, "budget_spent": False}
    if receipt is not None:
        counts["scope"] = receipt
    budget = measure_runs.RequestBudget(budget_requests)
    for _ in range(max_windows):
        if budget.spent():
            counts["budget_spent"] = True
            break
        state = measure_runs.process_once(model=model, client=client, budget=budget, scope=scope,
                                          with_rules=with_rules)["state"]
        counts["windows"][state] = counts["windows"].get(state, 0) + 1
        if state == "idle":
            break
    counts["requests"] = None if budget_requests is None else budget_requests - budget.left
    with history.history_engine().begin() as conn:
        counts["corrected_versions"] = len(measurements.scan_corrections(conn, _sources(conn)))
    return counts


def run_scope(scope, **kwargs) -> dict:
    """Measure an explicit list: rounds until no window of it is due (at most MAX_SCOPE_ROUNDS). The first round's
    receipt says what was planned; the total of model requests sent is the usage of the call."""
    rounds = []
    for _ in range(MAX_SCOPE_ROUNDS):
        counts = run_round(scope=scope, **kwargs)
        rounds.append(counts)
        if counts.get("state") == "disabled" or counts["windows"].get("idle"):
            break
    return {"scope_sha256": scope.sha256, "rounds": rounds, "requests": sum(r.get("requests") or 0 for r in rounds)}


def run_worker(*, stop, idle_seconds=30.0, rounds=None):
    n = 0
    while not stop.is_set() and (rounds is None or n < rounds):
        n += 1
        counts = run_round()
        if counts.get("state") == "disabled" or counts["windows"].get("idle"):
            stop.wait(idle_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measurement worker, or one explicit list with --scope")
    parser.add_argument("--scope", help="a JSON file listing the versions (and windows) to measure, once")
    parser.add_argument("--upgrade", action="store_true",
                        help="with --scope: also plan a listed version that only older producers read")
    args = parser.parse_args(argv)
    if args.upgrade and not args.scope:
        parser.error("--upgrade needs --scope")
    if measure_runs.model_from_env() is None and not measure_runs.rules_from_env():
        print(json.dumps({"measure_extract": {"state": "disabled"}}))
    elif args.scope:
        with open(args.scope, encoding="utf-8") as f:
            scope = measure_runs.Scope.parse(json.load(f))
        print(json.dumps(run_scope(scope, producer_upgrade=args.upgrade), ensure_ascii=False, default=str))
    else:
        import signal
        import threading

        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        started = time.monotonic()
        run_worker(stop=stop)
        print(json.dumps({"measure_extract": {"state": "stopped", "seconds": round(time.monotonic() - started)}}))


if __name__ == "__main__":
    main()

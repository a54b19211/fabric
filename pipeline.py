"""
==========================================================
  ZK_Data Fabric — 一份资料从抓取到能被检索，走哪几步
==========================================================

  这个文件是给人读、给单次调试用的：从上往下，一步一个函数，main() 按顺序调用。
  每个函数只调用主线里已有的函数，不另写第二套逻辑。

  一份资料走的路，和每一步存在哪张表里：

    1. fetch    按站点配置抓网页或接口      → history_runs（这次抓取的记录）、history_versions（原件原样存下，只追加）
    2. clean    出干净正文、找编号、做画像   → derived_clean_text、derived_identifiers、derived_profiles
    3. align    药名对到标准编号（徐子雯）   → （接入后才有）
    4. score    五维质量分和等级（徐子雯）   → （接入后才有）
    5. index    清洗正文送进知识库           → index_publications（待办），正文进 WeKnora
    6. measure  从正文里抽数字读数           → derived_measure_*（读数）、derived_measurements（读数对应的对象）
                                              核验服务对读数的核对由 db/extraction_checks.py 另一个程序做，这里不跑

  线上不是这样跑的：每一步由常驻的后台程序分段跑，靠表里的待办衔接，互不等待——
    fetch：flows/history_fetch.py    clean：flows/clean_stage.py
    index：migration/index_publication.py worker    measure：flows/measure_extract.py
  这里把同样的函数按顺序串起来，为的是读懂和对单个站点试跑；线上仍以后台程序为准。

  运行方式（仓库根目录，用本仓的 .venv；要先设好 DATAFABRIC_HISTORY_DATABASE_URL 等环境变量）：
    python pipeline.py --site nmpa --max 5          # 抓 nmpa 5 条，然后后面各步都跑一遍
    python pipeline.py --only fetch --site nmpa     # 只跑某一步：fetch clean align score index measure
    python pipeline.py --only clean --run <运行号>  # 清洗之前某次抓取留下的版本（运行号在 fetch 时打印）
"""

import argparse
import time

from biz_rules.history_models import FetchRequest
from config.crawler_settings import SITE_PROFILES
from db import clean_stage, history, index_publication
from flows import measure_extract
from flows.history_fetch import history_fetch_job

STEPS = ("fetch", "clean", "align", "score", "index", "measure")


def fetch(site, max_records):
    """按站点配置抓取，原件原样存成固定版本，内容没变的不重复存。返回这次抓取的运行号。"""
    if site not in SITE_PROFILES:
        raise SystemExit(f"站点 {site!r} 不在站点配置里，可用：{', '.join(sorted(SITE_PROFILES))}")
    profile = SITE_PROFILES[site]
    # 查询词：接口站点填 records，网页站点填配置里的第一个列表入口网址
    query = "records" if profile.get("source_type") == "api" else profile["start_urls"][0]
    request = FetchRequest(source_id=site, query=query, max_records=max_records)
    run, _ = history.create_fetch(request, key=f"pipeline-{site}-{int(time.time())}")  # 先记一条抓取任务（run）
    history_fetch_job(run.id, run.attempt)  # 这里同步抓完；线上是放进队列，由采集工人去执行
    return run.id


def clean(run_id):
    """对这次抓取留下的版本，一次写好：干净正文、编号、画像。清洗规则在 processing/ 目录。"""
    if not run_id:
        raise SystemExit("清洗要知道处理哪次抓取留下的版本：请加 --run <运行号>")
    return clean_stage.process_versions(history.read_run(run_id).version_ids)


def align():
    """（接入徐子雯的对齐后才有）药名 → PubChem/ChEMBL 标准编号。"""
    return "未接入"


def score():
    """（接入徐子雯的评分后才有）五维质量分和等级。"""
    return "未接入"


def index(rounds=20):
    """把清洗正文送进知识库（WeKnora）。没配置知识库时什么也不做；最多处理 rounds 个到期的待办。"""
    if not index_publication.configured():
        return "未配置知识库，跳过"
    states = []
    for _ in range(rounds):
        states.append(index_publication.process_once()["state"])
        if states[-1] == "idle":
            break
    return states


def measure():
    """从清洗正文里抽读数，读数落进表里。没配置模型或规则路线时什么也不做。"""
    return measure_extract.run_round()


def main(site, max_records, only=None, run_id=None):
    def on(step):
        return only is None or only == step

    if on("fetch"):
        run_id = fetch(site, max_records)
        print("fetch   运行号", run_id)
    if on("clean"):
        print("clean  ", clean(run_id))
    if on("align"):
        print("align  ", align())
    if on("score"):
        print("score  ", score())
    if on("index"):
        print("index  ", index())
    if on("measure"):
        print("measure", measure())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZK_Data Fabric：一份资料从抓取到能被检索，按顺序走一遍")
    parser.add_argument("--site", help="站点标识（config/sites 里配置的），如 nmpa；跑 fetch 时必填")
    parser.add_argument("--max", type=int, default=5, dest="max_records", help="最多抓几条（1~100），默认 5")
    parser.add_argument("--only", choices=STEPS, help="只跑这一步")
    parser.add_argument("--run", help="清洗用：之前某次抓取的运行号（单独跑 clean 时必填）")
    args = parser.parse_args()
    main(args.site, args.max_records, args.only, args.run)

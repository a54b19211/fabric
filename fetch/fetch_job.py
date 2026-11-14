"""采集任务的后台作业：一条采集任务（fabric_runs 的一行）→ 读公开来源 → 存成版本。

run_fetch_job 是放在 extraction 队列里的 RQ 作业，由采集工人（rq worker ... extraction，见
ops/systemd/datafabric-fetch-worker.service）取出来执行。读来源是 fetch/sources.py 的 read_public，存版本是
store/records.py 的 finish_fetch。队列的 Redis 地址单独配置（DATAFABRIC_REDIS_URL）。
"""

import os

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

from fetch.sources import read_public
from store import records
from store.record_models import FabricError

# 一次读取最多 120 秒（FetchRequest.deadline_seconds），截止时间只在两条之间查：给浏览器页面留出余量
JOB_TIMEOUT = 300


def fetch_queue():
    url = os.environ.get("DATAFABRIC_REDIS_URL", "")
    if not url:
        raise FabricError("fetch_queue_not_configured", "采集队列尚未配置", 503)
    return Queue("extraction", connection=Redis.from_url(url))


def enqueue_fetch(run):
    """把建好的采集任务放进队列（已经在队列里的不再放）；放不进去，任务记为失败。"""
    job_id = f"fetch-{run.id}-{run.attempt}"
    try:
        queue = fetch_queue()
        try:
            Job.fetch(job_id, connection=queue.connection)
        except NoSuchJobError:
            queue.enqueue(run_fetch_job, run.id, run.attempt, job_id=job_id, job_timeout=JOB_TIMEOUT,
                          result_ttl=86400, failure_ttl=604800)
    except Exception:  # noqa: BLE001 -- 队列不可用：不回显连接串
        records.fail_job(run.id, run.attempt, "enqueue_failed", "采集任务没能放进队列")
        raise FabricError("enqueue_failed", "采集任务没能放进队列", 503) from None
    return records.read_run(run.id)


def run_fetch_job(run_id: str, attempt: int):
    """RQ 作业本身：取任务 → 读来源 → 存版本。读不了的原因记在任务上；别的异常（程序错误）也把任务记成失败，
    不让它一直停在 running。"""
    try:
        request = records.job_input(run_id, attempt)
        result = records.finish_fetch(run_id, attempt, read_public(request))
        return {"run_id": result.id, "state": result.state, "attempt": attempt}
    except FabricError as exc:
        if exc.code != "stale_attempt":
            records.fail_job(run_id, attempt, exc.code, exc.message)
        raise
    except Exception as exc:
        records.fail_job(run_id, attempt, "job_crashed", type(exc).__name__)
        raise

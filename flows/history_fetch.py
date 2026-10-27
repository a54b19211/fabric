"""一个固定的公开读取RQ作业；沿用extraction队列，但连接必须显式独立配置。"""

import os

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

from biz_rules.history_models import HistoryError
from biz_rules.history_sources import read_public
from db.history import (
    enqueue_failed,
    fail_job,
    finish_fetch,
    job_input,
    read_run,
    retry_fetch,
    run_job_id,
)


def history_queue():
    url = os.environ.get("DATAFABRIC_HISTORY_REDIS_URL", "")
    if not url:
        raise HistoryError("history_queue_not_configured", "历史读取队列尚未配置", 503)
    return Queue("extraction", connection=Redis.from_url(url))


def enqueue_fetch(run):
    queue = history_queue()
    job_id = run_job_id(run.id)
    try:
        # 请求结果未知时同一个job_id先核对；不盲重发外部/保存工作。
        try:
            existing = Job.fetch(job_id, connection=queue.connection)
        except NoSuchJobError:
            existing = None
        if existing is None:
            queue.enqueue(
                history_fetch_job,
                run.id,
                run.attempt,
                job_id=job_id,
                job_timeout=180,
                result_ttl=86400,
                failure_ttl=604800,
            )
    except Exception:  # noqa: BLE001 -- 入队结果不明必须先核对，不能泄密或自动重发
        enqueue_failed(run.id, run.attempt)
        raise HistoryError(
            "enqueue_unknown", "队列提交结果未知；请按任务ID核对后恢复", 503
        ) from None
    return read_run(run.id)


def history_fetch_job(run_id: str, attempt: int):
    try:
        request = job_input(run_id, attempt)
        batch = read_public(request)
        result = finish_fetch(run_id, attempt, batch)
        return {"run_id": result.id, "state": result.state, "attempt": attempt}
    except HistoryError as exc:
        # 只保存安全业务错误；DB未知提交结果不在此捕获，不覆写已完成状态。
        if exc.code != "stale_attempt":
            fail_job(run_id, attempt, exc.code, exc.message)
        raise


def retry_inactive(run_id: str, expected_attempt: int):
    queue = history_queue()
    job_id = run_job_id(run_id)
    try:
        job = Job.fetch(job_id, connection=queue.connection)
        status = job.get_status(refresh=True)
        inactive = status in ("failed", "finished", "stopped", "canceled")
    except NoSuchJobError:
        inactive = True
    except Exception:  # noqa: BLE001 -- 核对失败一律拒绝重发
        raise HistoryError(
            "queue_status_unknown", "无法核对原任务状态，不能重发", 503
        ) from None
    run = retry_fetch(run_id, expected_attempt, job_inactive=inactive)
    return enqueue_fetch(run)

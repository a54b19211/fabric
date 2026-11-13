"""采集任务的后台作业：按一条采集任务（history_runs 里的一行）去读公开来源，把读到的原件存成固定版本。
本文件没有自己的进程：history_fetch_job 是放在 extraction 队列里的一个 RQ 作业，由采集工人
（rq worker ... extraction，见 ops/systemd/datafabric-history-worker.service）取出来执行。

从哪读起：先读 history_fetch_job（作业本身：取任务、读来源、存版本），再读 enqueue_fetch（建好任务后放进队列）和 retry_inactive
（原作业已经停了就重新放进队列）。读来源在 fetch/sources.py 的 read_public，网站和站点接口由 fetch/site_crawler.py
读；存版本在 store/history.py 的 finish_fetch。freeze_demo_fetch、read_demo_fetch 是隔离模拟补采，只读本地的模拟原件包，不碰外网。
队列沿用 extraction 队列，但 Redis 连接必须显式独立配置（DATAFABRIC_HISTORY_REDIS_URL）。

读写的表：本文件自己不写 SQL，都经 store/history.py：读写 history_runs（任务状态、版本号列表、覆盖情况），写 history_versions
（固定版本，原件原样、只追加）、history_attachment_links（附件和父页面的关系）、history_version_rank，并在同一事务里写
derived_registry_diff_runs 和 derived_registry_changes（登记差分），更新 history_sources 的 published_seq。
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from fetch.sources import parse_json, project_record, read_public, require_public_fetch_enabled
from store.history import (
    enqueue_failed,
    fail_job,
    finish_fetch,
    job_input,
    read_run,
    retry_fetch,
    run_job_id,
)
from store.history_models import (
    MAX_IMPORT_BYTES,
    ArchiveRecord,
    HistoryError,
    canonical,
    sha256,
)


def require_demo_fetch(request=None):
    """A server-owned declaration binds this process to one isolated test backend."""
    try:
        config = json.loads(os.environ.get("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", ""))
        database, redis = config["database"], config["redis"]
        if (not isinstance(config["instance_id"], str) or not config["instance_id"]
                or not Path(config["input_file"]).is_absolute()
                or not isinstance(database["host"], str) or not isinstance(redis["host"], str)
                or not isinstance(database["port"], int) or isinstance(database["port"], bool)
                or not isinstance(redis["port"], int) or isinstance(redis["port"], bool)
                or not isinstance(redis["db"], int) or isinstance(redis["db"], bool)
                or not 1 <= database["port"] <= 65535 or not 1 <= redis["port"] <= 65535
                or not 0 <= redis["db"] <= 15 or not database["name"]):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HistoryError("demo_instance_not_configured", "未配置明确隔离的模拟补采实例", 403) from None
    try:
        actual_database = make_url(os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL", ""))
        actual_redis = urlsplit(os.environ.get("DATAFABRIC_HISTORY_REDIS_URL", ""))
        redis_db = int(actual_redis.path.lstrip("/") or "0")
        local = {"127.0.0.1", "::1", "localhost"}
        if ((request is not None and (not request.is_demo or request.demo_instance_id != config["instance_id"]))
                or database["host"] not in local or redis["host"] not in local
                or actual_database.get_backend_name() != "postgresql" or actual_database.query
                or (actual_database.host, actual_database.port or 5432, actual_database.database)
                   != (database["host"], database["port"], database["name"])
                or actual_redis.scheme not in {"redis", "rediss"}
                or actual_redis.query or actual_redis.fragment
                or (actual_redis.hostname, actual_redis.port or 6379, redis_db)
                   != (redis["host"], redis["port"], redis["db"])):
            raise ValueError
    except (ValueError, TypeError, ArgumentError):
        raise HistoryError("demo_instance_mismatch", "模拟实例或隔离数据库、队列身份不匹配", 403) from None
    return config


def require_fetch_allowed(request):
    if request.is_demo:
        return require_demo_fetch(request)
    require_public_fetch_enabled()


def require_retry_allowed():
    # Default/live instances keep the original early closure, without a DB or
    # Redis read. Demo instances must prove their backend binding before lookup.
    if os.environ.get("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", ""):
        require_demo_fetch()
    else:
        require_public_fetch_enabled()


def freeze_demo_fetch(request):
    """Freeze local synthetic originals before accepting the existing durable fetch run."""
    config = require_demo_fetch(request)
    try:
        with Path(config["input_file"]).open("rb") as source:
            raw = source.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise ValueError
        package = parse_json(raw.decode("utf-8"))
        if (package.get("schema_version") != "datafabric.demo_fetch.v1"
                or package.get("instance_id") != request.demo_instance_id
                or not isinstance(package.get("sources"), dict)
                or any(not isinstance(items, list) for items in package["sources"].values())
                or sum(len(items) for items in package["sources"].values()) > 5000):
            raise ValueError
        items = [ArchiveRecord.model_validate(value)
                 for value in package["sources"].get(request.source_id, [])]
        if not items or len({item.source_record_id for item in items}) != len(items):
            raise ValueError
        projections = [(item, project_record(request.source_id, item)) for item in items]
    except (OSError, ValueError, TypeError, KeyError, HistoryError):
        raise HistoryError("demo_input_invalid", "模拟原件包缺失、超过预算或不符合来源契约", 422) from None
    ids = set(request.source_record_ids)
    if ids and not ids <= {item.source_record_id for item in items}:
        raise HistoryError("demo_no_records", "指定模拟记录不在隔离原件包内", 422)
    terms = [] if request.query.strip() == "*" else request.query.casefold().split()
    matched = []
    for item, projected in projections:
        haystack = "\n".join((item.source_record_id, projected.get("title", ""),
                               projected.get("clean_text", ""))).casefold()
        if (not ids or item.source_record_id in ids) and all(term in haystack for term in terms):
            matched.append(item)
    selected = [item.model_dump(mode="json") for item in matched[:request.max_records]]
    return {
        "schema_version": "datafabric.demo_fetch.frozen.v1",
        "demo_instance_id": request.demo_instance_id,
        "request_sha256": sha256(canonical(request.model_dump(mode="json", exclude_none=True))),
        "records": selected, "input_sha256": sha256(canonical(selected)),
        "source_total": len(matched), "available_count": len(items),
        "stop_reason": "record_budget" if len(matched) > len(selected)
                       else "demo_end_of_package" if matched else "demo_query_no_match",
    }


def read_demo_fetch(request, frozen):
    """No URL handling or external reader exists on the demo path."""
    require_demo_fetch(request)
    try:
        if (not isinstance(frozen, dict)
                or frozen.get("schema_version") != "datafabric.demo_fetch.frozen.v1"
                or frozen.get("demo_instance_id") != request.demo_instance_id
                or frozen.get("request_sha256") != sha256(canonical(request.model_dump(mode="json", exclude_none=True)))
                or frozen.get("input_sha256") != sha256(canonical(frozen["records"]))
                or not 0 < frozen["available_count"] <= 5000
                or not isinstance(frozen["records"], list)
                or len(frozen["records"]) > request.max_records
                or not 0 <= frozen["source_total"] <= frozen["available_count"]):
            raise ValueError
        records = [ArchiveRecord.model_validate(value) for value in frozen["records"]]
        expected = min(frozen["source_total"], request.max_records)
        stop = ("record_budget" if frozen["source_total"] > expected else
                "demo_end_of_package" if expected else "demo_query_no_match")
        if len(records) != expected or frozen["stop_reason"] != stop:
            raise ValueError
        for item in records:
            project_record(request.source_id, item)
    except (ValueError, TypeError, KeyError, HistoryError):
        raise HistoryError("demo_input_invalid", "冻结的模拟补采输入不完整或校验失败", 422) from None
    return {
        "records": records, "complete": False,
        "upstream_pagination_exhausted": len(records) == frozen["source_total"],
        "stop_reason": frozen["stop_reason"], "requests": 0,
        "bytes": sum(len(item.raw_content.encode("utf-8")) for item in records),
        "source_total": frozen["source_total"], "error": None,
        "demo_instance_id": request.demo_instance_id, "input_sha256": frozen["input_sha256"],
    }


def history_queue():
    url = os.environ.get("DATAFABRIC_HISTORY_REDIS_URL", "")
    if not url:
        raise HistoryError("history_queue_not_configured", "历史读取队列尚未配置", 503)
    return Queue("extraction", connection=Redis.from_url(url))


def enqueue_fetch(run):
    require_fetch_allowed(run)
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
                **({"demo_instance_id": run.demo_instance_id} if run.is_demo else {}),
            )
    except Exception:  # noqa: BLE001 -- 入队结果不明必须先核对，不能泄密或自动重发
        enqueue_failed(run.id, run.attempt)
        raise HistoryError(
            "enqueue_unknown", "队列提交结果未知；请按任务ID核对后恢复", 503
        ) from None
    return read_run(run.id)


def history_fetch_job(run_id: str, attempt: int, demo_instance_id: str | None = None):
    # This identity travels in the durable RQ job. Reject backend drift before even
    # reading a run or trying to write a failure to a possibly different database.
    if demo_instance_id is not None:
        require_demo_fetch(SimpleNamespace(is_demo=True, demo_instance_id=demo_instance_id))
    try:
        request, frozen = job_input(run_id, attempt)
        require_fetch_allowed(request)
        if demo_instance_id is not None and request.demo_instance_id != demo_instance_id:
            raise HistoryError("fetch_identity_mismatch", "队列任务与模拟实例不匹配", 409)
        batch = read_demo_fetch(request, frozen) if request.is_demo else read_public(request)
        result = finish_fetch(run_id, attempt, batch)
        return {"run_id": result.id, "state": result.state, "attempt": attempt}
    except HistoryError as exc:
        # 只保存安全业务错误；DB未知提交结果不在此捕获，不覆写已完成状态。
        if exc.code != "stale_attempt":
            fail_job(run_id, attempt, exc.code, exc.message)
        raise


def retry_inactive(run_id: str, expected_attempt: int):
    require_retry_allowed()
    require_fetch_allowed(read_run(run_id))
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

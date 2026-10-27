"""抽取运行保护：调用实际应用/队列代码，仅替换外部 I/O，不连接服务。"""

import builtins
import importlib
import inspect
import socket
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
from fastapi.testclient import TestClient

SECRET = "redis://private-user:DO-NOT-EXPOSE@private-host:6379/8"


class RedisIO:
    """RQ Queue 使用真实实现；Redis 命令在此返回受控结果或连接错误。"""

    def __init__(self):
        self.pending = ["already-queued", "still-running-reference"]
        self.values = {}
        self.failures = {}

    def _check(self, command):
        if command in self.failures:
            raise self.failures[command]

    def llen(self, key):
        self._check("llen")
        return len(self.pending)

    def get(self, key):
        self._check("get")
        return self.values.get(key)

    def set(self, key, value):
        self._check("set")
        self.values[key] = value
        return True

    def delete(self, key):
        self._check("delete")
        return int(self.values.pop(key, None) is not None)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("runtime safety tests must not connect to a server")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def runtime(monkeypatch):
    # 普通导入实际业务模块，避免复制函数或用替身 router 测试自己。
    app = importlib.import_module("api.main").app
    task_queue = importlib.import_module("core_engine.task_queue")
    worker = importlib.import_module("flows.extraction_worker")
    job = importlib.import_module("flows.extraction_rq_job")
    flow = importlib.import_module("flows.extraction_flow")
    repository = importlib.import_module("db.repository")
    session = importlib.import_module("db.session")
    redis = importlib.import_module("redis")
    rq = importlib.import_module("rq")

    monkeypatch.setattr(task_queue.APP, "TASK_QUEUE_BACKEND", "redis")
    monkeypatch.setattr(task_queue.APP, "REDIS_URL", "redis://unused.invalid:1/15")
    monkeypatch.setattr(task_queue.APP, "EXTRACTION_CONCURRENCY", 2)
    monkeypatch.setattr(task_queue, "_queue_instance", None)
    monkeypatch.setattr(worker, "_manager_instance", None)
    engine = Mock(side_effect=AssertionError("unexpected database access"))
    monkeypatch.setattr(session, "get_engine", engine)
    redis_io = RedisIO()
    redis_connection = Mock(return_value=redis_io)
    monkeypatch.setattr(redis.Redis, "from_url", redis_connection)

    return SimpleNamespace(
        app=app, task_queue=task_queue, worker=worker, job=job, flow=flow,
        repository=repository, session=session, redis=redis, rq=rq,
        redis_io=redis_io, redis_connection=redis_connection, engine=engine,
    )


def test_startup_and_first_status_query_preserve_existing_work(runtime, monkeypatch):
    reset = Mock(side_effect=AssertionError("startup must not reset database state"))
    clear = Mock(side_effect=AssertionError("startup must not clear Redis"))
    recover = Mock(side_effect=AssertionError("query must not recover in-flight jobs"))
    monkeypatch.setattr(runtime.repository, "reset_stale_extractions", reset)
    monkeypatch.setattr(runtime.task_queue, "clear_extraction_redis", clear)
    monkeypatch.setattr(runtime.task_queue.RedisTaskQueue, "recover_pending", recover)
    before = list(runtime.redis_io.pending)

    with TestClient(runtime.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert runtime.worker._manager_instance is None
        response = client.get("/api/extraction/workers")

    assert response.status_code == 200
    assert response.json() == {
        "num_workers": 2, "total_pending": 2, "paused": False,
        "workers": [{"worker_id": 0, "queue_size": 2}],
    }
    assert isinstance(runtime.worker._manager_instance, runtime.worker.RedisWorkerManager)
    assert runtime.redis_io.pending == before
    reset.assert_not_called()
    clear.assert_not_called()
    recover.assert_not_called()
    runtime.engine.assert_not_called()

    from sqlalchemy import event

    from db.models import ExtractedEntity
    from db.review_listeners import _guard_direct_status_write

    assert event.contains(ExtractedEntity.review_status, "set", _guard_direct_status_write)


@pytest.mark.parametrize(
    "method,path,command",
    [
        ("get", "/workers", "llen"),
        ("get", "/workers", "get"),
        ("post", "/pause", "set"),
        ("post", "/resume", "delete"),
    ],
)
def test_redis_failure_http_is_non_success_and_does_not_expose_connection(
    runtime, method, path, command,
):
    responses = []
    with TestClient(runtime.app) as client:
        for detail in (SECRET, "a different private database credential"):
            runtime.redis_io.failures[command] = runtime.redis.exceptions.ConnectionError(detail)
            response = client.request(method, "/api/extraction" + path)
            assert response.status_code == 500
            assert detail not in response.text
            assert "total_pending" not in response.json()
            responses.append(response.json())
    assert responses[0] == responses[1]
    runtime.engine.assert_not_called()


@pytest.mark.parametrize(
    "method,path,dependency,status",
    [
        ("get", "/workers", "redis", 503),
        ("get", "/workers", "rq", 503),
        ("post", "/pause", "redis", 500),
        ("post", "/resume", "redis", 500),
    ],
)
def test_missing_queue_dependency_is_not_reported_as_an_empty_healthy_queue(
    runtime, monkeypatch, method, path, dependency, status,
):
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == dependency:
            raise ImportError(SECRET)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with TestClient(runtime.app) as client:
        response = client.request(method, "/api/extraction" + path)
    assert response.status_code == status
    assert SECRET not in response.text
    assert "total_pending" not in response.json()
    runtime.engine.assert_not_called()


def test_redis_pause_resume_success_preserves_queue_and_reports_actual_flag(runtime):
    before = list(runtime.redis_io.pending)
    with TestClient(runtime.app) as client:
        assert client.get("/api/extraction/workers").json()["paused"] is False
        paused = client.post("/api/extraction/pause")
        assert paused.status_code == 200
        assert paused.json()["paused"] is True
        assert runtime.redis_io.values[runtime.task_queue._EXTRACTION_PAUSE_KEY]
        assert client.get("/api/extraction/workers").json()["paused"] is True
        resumed = client.post("/api/extraction/resume")
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is False
        assert client.get("/api/extraction/workers").json()["paused"] is False
    assert runtime.redis_io.pending == before
    runtime.engine.assert_not_called()


def test_reset_stale_rejects_every_integer_threshold_without_database_writes(runtime, monkeypatch):
    reset = Mock(side_effect=AssertionError("unsafe bulk reset"))
    monkeypatch.setattr(runtime.repository, "reset_stale_extractions", reset)
    with TestClient(runtime.app) as client:
        for threshold in (None, -100, 0, 1, 30, 300, 10**30):
            params = {} if threshold is None else {"max_age_minutes": threshold}
            response = client.post("/api/extraction/reset-stale", params=params)
            assert response.status_code == 409
            assert "未修改" in response.json()["detail"]
        operation = client.get("/openapi.json").json()["paths"][
            "/api/extraction/reset-stale"
        ]["post"]
    assert "409" in operation["responses"]
    assert "200" not in operation["responses"]
    parameter = next(p for p in operation["parameters"] if p["name"] == "max_age_minutes")
    assert parameter["schema"]["default"] == 30
    reset.assert_not_called()
    runtime.engine.assert_not_called()
    runtime.redis_connection.assert_not_called()


def test_memory_queue_fifo_and_empty_behavior_are_preserved(runtime):
    queue = runtime.task_queue.MemoryTaskQueue()
    assert queue.dequeue(timeout=0) is None
    first = runtime.task_queue.Task(uuid="memory-first")
    second = runtime.task_queue.Task(uuid="memory-second")
    queue.enqueue(first)
    queue.enqueue(second)
    assert queue.size() == 2
    assert queue.dequeue(timeout=0) is first
    queue.task_done()
    assert queue.dequeue(timeout=0) is second
    queue.task_done()
    assert queue.size() == 0
    assert queue.recover_pending() == []
    queue.shutdown()
    assert queue.dequeue(timeout=0) is None
    queue.task_done()
    runtime.redis_connection.assert_not_called()


def test_memory_manager_http_pause_resume_does_not_require_redis(runtime, monkeypatch):
    monkeypatch.setattr(runtime.task_queue.APP, "TASK_QUEUE_BACKEND", "memory")
    try:
        with TestClient(runtime.app) as client:
            response = client.get("/api/extraction/workers")
            assert response.status_code == 200
            assert response.json()["num_workers"] == 2
            assert response.json()["total_pending"] == 0
            assert client.post("/api/extraction/pause").status_code == 200
            assert client.get("/api/extraction/workers").json()["paused"] is True
            assert client.post("/api/extraction/resume").status_code == 200
            assert client.get("/api/extraction/workers").json()["paused"] is False
            # 共享 Redis 暂停帮助函数在 memory 模式仍是无操作。
            runtime.task_queue.set_extraction_paused(True)
            assert runtime.task_queue.is_extraction_paused() is False
    finally:
        if runtime.worker._manager_instance is not None:
            runtime.worker._manager_instance.shutdown()
    runtime.redis_connection.assert_not_called()
    runtime.engine.assert_not_called()


def test_redis_submission_keeps_the_existing_rq_function_and_uuid_contract(runtime):
    uuid = "existing-document-uuid:unchanged"
    with patch.object(runtime.rq.Queue, "enqueue", autospec=True) as enqueue:
        manager = runtime.worker.get_extraction_manager()
        assert manager.submit(runtime.worker.ExtractionTask(uuid=uuid)) == 0
    enqueue.assert_called_once()
    queue, function, submitted_uuid = enqueue.call_args.args
    assert isinstance(queue, runtime.rq.Queue)
    assert queue.name == "extraction"
    assert queue.connection is runtime.redis_io
    assert function is runtime.job.extraction_job
    assert function.__module__ + "." + function.__name__ == "flows.extraction_rq_job.extraction_job"
    assert list(inspect.signature(function).parameters) == ["uuid"]
    assert submitted_uuid == uuid
    assert enqueue.call_args.kwargs == {"job_timeout": 18000}


@pytest.mark.parametrize("database_write_fails", [False, True])
def test_pause_preflight_failure_records_failed_then_raises_without_starting_extraction(
    runtime, monkeypatch, database_write_fails,
):
    runtime.redis_io.failures["get"] = runtime.redis.exceptions.ConnectionError(SECRET)
    update = Mock(side_effect=RuntimeError("private database write failure") if database_write_fails else None)
    extract = Mock(side_effect=AssertionError("must not start extraction"))
    get_session = Mock(side_effect=AssertionError("must not read document before preflight succeeds"))
    sleep = Mock(side_effect=AssertionError("a failed read is not a pause result"))
    monkeypatch.setattr(runtime.repository, "update_extraction_status", update)
    monkeypatch.setattr(runtime.flow, "extract_single_document", extract)
    monkeypatch.setattr(runtime.session, "get_session", get_session)
    monkeypatch.setattr(runtime.job.time, "sleep", sleep)

    with pytest.raises(RuntimeError) as error:
        runtime.job.extraction_job("not-started-uuid")
    assert "暂停" in str(error.value)
    assert SECRET not in str(error.value)
    assert "private database write failure" not in str(error.value)
    update.assert_called_once()
    assert update.call_args.args == ("not-started-uuid", "failed")
    assert "尚未开始" in update.call_args.kwargs["error"]
    assert SECRET not in update.call_args.kwargs["error"]
    extract.assert_not_called()
    get_session.assert_not_called()
    sleep.assert_not_called()


def test_existing_rq_job_waits_for_resume_then_extracts_with_the_original_uuid(runtime, monkeypatch):
    uuid = "legacy-rq-job-uuid"
    update = Mock()
    extract = Mock(return_value=True)
    session = Mock()
    session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        raw_content="frozen source text", source_site="test-source", doc_type="article",
    )

    @contextmanager
    def get_session():
        yield session

    def sleep(seconds):
        assert seconds == 2
        update.assert_not_called()
        extract.assert_not_called()

    monkeypatch.setattr(runtime.repository, "update_extraction_status", update)
    monkeypatch.setattr(runtime.session, "get_session", get_session)
    monkeypatch.setattr(runtime.flow, "extract_single_document", extract)
    monkeypatch.setattr(runtime.task_queue, "is_extraction_paused", Mock(side_effect=[True, False]))
    monkeypatch.setattr(runtime.job.time, "sleep", sleep)

    assert runtime.job.extraction_job(uuid) is True
    assert update.call_args_list == [call(uuid, "processing")]
    extract.assert_called_once_with(
        doc_uuid=uuid, raw_content="frozen source text", source_site="test-source", doc_type="article",
    )

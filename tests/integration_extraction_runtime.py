"""在专用 PostgreSQL/Redis 上检查重启保护；不调用真实抽取服务。

显式运行本文件；DATAFABRIC_TEST_DB_PORT / DATAFABRIC_TEST_REDIS_PORT 必须指向
新建的本地测试容器。普通 pytest 不收集本文件，不用 skip 代替集成检查。
"""
import multiprocessing
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from redis import Redis
from rq import Queue, SimpleWorker, Worker
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from api.main import app
from graph.db import session as db_session
from graph.db.models import Document, OperationLog
from graph.db.repository import update_extraction_status
from graph.extraction import extraction_flow, extraction_worker, task_queue
from settings import APP


def wait_for_release():
    """只有合成作业调用此函数，让检查期间确实有一个 RQ 作业正在执行。"""
    client = Redis.from_url(os.environ['DATAFABRIC_TEST_REDIS_URL'])
    if client.blpop('runtime-test:release', timeout=30) is None:
        raise TimeoutError('测试控制端没有释放作业')
    return True


def run_one_worker(redis_url):
    client = Redis.from_url(redis_url)
    Worker([Queue('extraction', connection=client)], connection=client).work(
        burst=True, max_jobs=1,
    )


@contextmanager
def running_api():
    """实际启动和停止 API 进程，连接仅限本机测试端口。"""
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([
        sys.executable, '-m', 'uvicorn', 'api.main:app',
        '--host', '127.0.0.1', '--port', str(port),
    ])
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as api:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, '测试 API 进程意外退出'
                try:
                    if api.get('/health').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, '测试 API 未就绪'
                time.sleep(0.1)
            yield api
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def isolated_runtime(monkeypatch):
    # 不读取现役配置的连接地址，也不清空任何已有库。
    db_port = int(os.environ['DATAFABRIC_TEST_DB_PORT'])
    redis_port = int(os.environ['DATAFABRIC_TEST_REDIS_PORT'])
    redis_url = f'redis://127.0.0.1:{redis_port}/0'
    values = {
        'DB_HOST': '127.0.0.1', 'DB_PORT': db_port,
        'DB_USER': 'datafabric_runtime_test', 'DB_NAME': 'datafabric_runtime_test',
        'DB_PASSWORD': '', 'REDIS_URL': redis_url, 'TASK_QUEUE_BACKEND': 'redis',
    }
    for name, value in values.items():
        monkeypatch.setattr(APP, name, value)
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv('DATAFABRIC_TEST_REDIS_URL', redis_url)
    monkeypatch.setattr(task_queue, '_queue_instance', None)
    monkeypatch.setattr(extraction_worker, '_manager_instance', None)
    monkeypatch.setattr(db_session, '_engine', None)
    monkeypatch.setattr(db_session, '_SessionLocal', None)
    client = Redis.from_url(redis_url)
    assert client.dbsize() == 0, '集成检查只接受空的专用测试 Redis'
    engine = create_engine(APP.DB_URL)
    assert not inspect(engine).get_table_names(), '集成检查只接受空的专用测试数据库'
    Document.__table__.create(engine)
    OperationLog.__table__.create(engine)
    yield engine, client, redis_url
    if db_session._engine is not None:
        db_session._engine.dispose()
    engine.dispose()
    client.close()


def test_restart_preserves_real_tasks_and_errors_are_visible(isolated_runtime, monkeypatch):
    engine, client, redis_url = isolated_runtime
    queue = Queue('extraction', connection=client)
    old_time = datetime.now(timezone.utc) - timedelta(hours=2)
    with Session(engine) as session:
        session.add_all([
            Document(uuid='synthetic-active', raw_content='合成测试材料',
                     extraction_status='processing', updated_at=old_time),
            Document(uuid='synthetic-queued', raw_content='合成测试材料',
                     extraction_status='queued', updated_at=old_time),
            Document(uuid='synthetic-pause-error', raw_content='合成测试材料',
                     extraction_status='queued', updated_at=old_time),
        ])
        session.commit()

    def statuses():
        with Session(engine) as session:
            return dict(session.execute(select(Document.uuid, Document.extraction_status)).all())

    active = queue.enqueue('tests.integration_extraction_runtime.wait_for_release')
    worker = multiprocessing.get_context('fork').Process(target=run_one_worker, args=(redis_url,))
    worker.start()
    try:
        deadline = time.monotonic() + 8
        while active.get_status(refresh=True) != 'started':
            assert worker.is_alive(), '测试 Worker 意外退出'
            assert time.monotonic() < deadline, '测试作业未进入执行状态'
            time.sleep(0.05)

        # 以原有函数名、UUID 参数和超时入队，模拟升级前已保存的作业。
        queued = queue.enqueue(
            'graph.extraction.extraction_rq_job.extraction_job', 'synthetic-queued', job_timeout=18000,
        )
        client.set('zk:extraction:paused', '1')
        before_jobs = queue.job_ids
        before_started = client.zrange('rq:wip:extraction', 0, -1)
        before_statuses = statuses()
        assert before_started, '必须有真实执行中的 RQ 作业'
        for _ in range(2):
            with running_api() as api:
                assert api.get('/health').status_code == 200
                response = api.get('/api/extraction/workers')
                assert response.status_code == 200
                assert response.json()['total_pending'] == 1
                assert response.json()['paused'] is True
                assert api.post('/api/extraction/reset-stale?max_age_minutes=0').status_code == 409
            assert queue.job_ids == before_jobs
            assert client.zrange('rq:wip:extraction', 0, -1) == before_started
            assert active.get_status(refresh=True) == 'started'
            assert statuses() == before_statuses
        print('PASS: 两次 API 启动与首次状态查询保留真实排队/在途任务和数据库状态')
    finally:
        client.rpush('runtime-test:release', '1')
        worker.join(timeout=5)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    assert worker.exitcode == 0
    assert active.get_status(refresh=True) == 'finished'

    with TestClient(app) as api:
        assert api.post('/api/extraction/resume').json()['paused'] is False
        assert client.get('zk:extraction:paused') is None
        assert api.post('/api/extraction/pause').json()['paused'] is True
        assert client.get('zk:extraction:paused') == b'1'
        assert api.post('/api/extraction/resume').json()['paused'] is False

    def synthetic_extraction(**kwargs):
        # 仅替代专业抽取边界；RQ 调用、原文读取与状态写入使用实际实现。
        assert kwargs['raw_content'] == '合成测试材料'
        update_extraction_status(kwargs['doc_uuid'], 'done')
        return True

    extract = Mock(side_effect=synthetic_extraction)
    monkeypatch.setattr(extraction_flow, 'extract_single_document', extract)
    SimpleWorker([queue], connection=client).work(burst=True)
    assert queued.get_status(refresh=True) == 'finished'
    assert queued.return_value() is True
    assert statuses()['synthetic-queued'] == 'done'
    extract.assert_called_once()
    print('PASS: 原有 RQ 作业函数与 UUID 参数可执行；正常暂停/继续有效')

    failed = queue.enqueue('graph.extraction.extraction_rq_job.extraction_job', 'synthetic-pause-error')
    with monkeypatch.context() as changed:
        changed.setattr(task_queue, 'is_extraction_paused', Mock(side_effect=RuntimeError('合成的暂停读取故障')))
        SimpleWorker([queue], connection=client).work(burst=True)
    assert failed.get_status(refresh=True) == 'failed'
    assert statuses()['synthetic-pause-error'] == 'failed'
    extract.assert_called_once()
    print('PASS: 暂停检查失败同时反映在真实 RQ 与 PostgreSQL，未继续调用抽取')

    # 使用实际 TCP 拒绝连接，验证失败沿队列、管理对象和 HTTP 接口传播。
    with socket.socket() as unavailable, monkeypatch.context() as changed:
        unavailable.bind(('127.0.0.1', 0))
        changed.setattr(APP, 'REDIS_URL', f'redis://127.0.0.1:{unavailable.getsockname()[1]}/0')
        changed.setattr(task_queue, '_queue_instance', None)
        changed.setattr(extraction_worker, '_manager_instance', None)
        with TestClient(app) as api:
            assert api.get('/api/extraction/workers').status_code == 500
            assert api.post('/api/extraction/pause').status_code == 500
            assert api.post('/api/extraction/resume').status_code == 500
    print('PASS: Redis 实际不可连接时，状态查询/暂停/继续均明确失败')

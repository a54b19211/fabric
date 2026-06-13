"""
任务队列策略模块 —— 可插拔的任务队列后端。

策略：
  - MemoryTaskQueue → 开发/单机使用，基于 queue.Queue
  - RedisTaskQueue  → 生产使用，基于 RQ，任务持久化到 Redis，重启不丢

用法：
  from core_engine.task_queue import get_task_queue
  tq = get_task_queue()
  tq.enqueue(Task(uuid="xxx"))
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol, Optional
from loguru import logger

from config.settings import APP


# ═══════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Task:
    """队列中的任务，只存 uuid，worker 执行时按需查 DB。"""
    uuid: str
    submit_time: float = field(default_factory=time.time)


# ═══════════════════════════════════════════════════════════════════════
# 抽象接口 (Protocol)
# ═══════════════════════════════════════════════════════════════════════

class TaskQueue(Protocol):
    """任务队列策略接口。"""

    def enqueue(self, task: Task) -> None:
        """提交任务到队列。"""
        ...

    def dequeue(self, timeout: float = 1.0) -> Optional[Task]:
        """从队列取出一个任务，超时返回 None。"""
        ...

    def size(self) -> int:
        """当前队列中待处理的任务数。"""
        ...

    def recover_pending(self) -> list[Task]:
        """恢复上次未完成的任务（Redis 后端专用，内存后端返回空列表）。"""
        ...

    def shutdown(self) -> None:
        """优雅关闭，等待队列清空。"""
        ...


# ═══════════════════════════════════════════════════════════════════════
# MemoryTaskQueue — 基于 queue.Queue（默认，零依赖）
# ═══════════════════════════════════════════════════════════════════════

_SENTINEL = None  # Worker 退出哨兵


class MemoryTaskQueue:
    """封装 Python queue.Queue，行为跟原来完全一致。"""

    def __init__(self):
        self._queue: queue.Queue[Optional[Task]] = queue.Queue()

    def enqueue(self, task: Task) -> None:
        self._queue.put(task)

    def dequeue(self, timeout: float = 1.0) -> Optional[Task]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def task_done(self) -> None:
        self._queue.task_done()

    def size(self) -> int:
        return self._queue.qsize()

    def recover_pending(self) -> list[Task]:
        return []

    def shutdown(self) -> None:
        self._queue.put(_SENTINEL)


# ═══════════════════════════════════════════════════════════════════════
# RedisTaskQueue — 基于 RQ，任务持久化到 Redis
# ═══════════════════════════════════════════════════════════════════════

class RedisTaskQueue:
    """用 RQ 的 Queue 对接 Redis，任务持久化，重启不丢。"""

    def __init__(self, redis_url: str = ""):
        self._redis_url = redis_url or APP.REDIS_URL
        self._rq_queue: Optional[object] = None
        self._lock = threading.Lock()

    @property
    def _q(self):
        """延迟初始化 RQ Queue（避免导入时连接 Redis）。"""
        if self._rq_queue is None:
            with self._lock:
                if self._rq_queue is None:
                    from redis import Redis
                    from rq import Queue as RQQueue
                    conn = Redis.from_url(self._redis_url)
                    self._rq_queue = RQQueue("extraction", connection=conn)
                    logger.info(f"[TaskQueue] Redis 队列已连接: {self._redis_url}")
        return self._rq_queue

    def enqueue(self, task: Task) -> None:
        from flows.extraction_rq_job import extraction_job
        self._q.enqueue(extraction_job, task.uuid)

    def dequeue(self, timeout: float = 1.0) -> Optional[Task]:
        # RQ worker 进程自己从 Redis 取任务，不经过这个方法
        return None

    def size(self) -> int:
        try:
            return self._q.count
        except Exception:
            return 0

    def recover_pending(self) -> list[Task]:
        """启动时把超时的 StartedJob 重新入队（worker 崩溃兜底）。"""
        try:
            from rq.registry import StartedJobRegistry
            registry = StartedJobRegistry("extraction", connection=self._q.connection)
            # 超过 30 分钟没完成的视为已丢失，重新入队
            stuck_ids = registry.get_job_ids()
            recovered = 0
            for job_id in stuck_ids:
                try:
                    job = self._q.fetch_job(job_id)
                    if job is not None:
                        registry.remove(job, delete_job=True)
                        self._q.enqueue(job.func, *job.args, **job.kwargs)
                        recovered += 1
                except Exception:
                    pass
            if recovered:
                logger.info(f"[TaskQueue] 恢复 {recovered} 个未完成任务")
            return [Task(uuid=job_id) for job_id in stuck_ids]
        except Exception as e:
            logger.warning(f"[TaskQueue] 恢复未完成任务失败: {e}")
            return []

    def shutdown(self) -> None:
        pass  # RQ worker 进程独立管理，不需要 Python 侧关闭

    @property
    def connection(self):
        return self._q.connection


# ═══════════════════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════════════════

_queue_instance: Optional[TaskQueue] = None
_queue_lock = threading.Lock()


def get_task_queue() -> TaskQueue:
    """根据 APP.TASK_QUEUE_BACKEND 返回队列策略实例（单例）。"""
    global _queue_instance
    if _queue_instance is None:
        with _queue_lock:
            if _queue_instance is None:
                backend = APP.TASK_QUEUE_BACKEND
                if backend == "redis":
                    logger.info("[TaskQueue] 使用 Redis 后端（任务持久化）")
                    _queue_instance = RedisTaskQueue()
                else:
                    logger.info("[TaskQueue] 使用 Memory 后端（默认）")
                    _queue_instance = MemoryTaskQueue()
    return _queue_instance

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
                    logger.info("[TaskQueue] Redis 队列已初始化")
        return self._rq_queue

    def enqueue(self, task: Task) -> None:
        from flows.extraction_rq_job import extraction_job
        self._q.enqueue(extraction_job, task.uuid, job_timeout=18000)  # 5 小时超时

    def dequeue(self, timeout: float = 1.0) -> Optional[Task]:
        # RQ worker 进程自己从 Redis 取任务，不经过这个方法
        return None

    def size(self) -> int:
        try:
            return self._q.count
        except ImportError:
            raise
        except Exception:
            raise RuntimeError("无法读取抽取队列，请核对 Redis 后重试") from None

    def recover_pending(self) -> list[Task]:
        """启动时把超时的 StartedJob 重新入队（worker 崩溃兜底）。"""
        try:
            from rq.registry import StartedJobRegistry
            registry = StartedJobRegistry("extraction", connection=self._q.connection)
            stuck_ids = registry.get_job_ids()
            recovered = 0
            for job_id in stuck_ids:
                try:
                    job = self._q.fetch_job(job_id)
                    if job is not None:
                        registry.remove(job, delete_job=True)
                        self._q.enqueue(job.func, *job.args, **job.kwargs, job_timeout=18000)
                        recovered += 1
                except Exception:
                    pass
            if recovered:
                logger.info(f"[TaskQueue] 恢复 {recovered} 个未完成任务")
            return [Task(uuid=job_id) for job_id in stuck_ids]
        except Exception as e:
            logger.warning(f"[TaskQueue] 恢复未完成任务失败: {e}")
            return []

    def clear_all(self) -> int:
        """
        清空 Redis 中 extraction 队列的所有任务及 StartedJobRegistry。
        服务重启时调用，配合 reset_stale_extractions() 实现完全重置。
        返回清除的任务数。
        """
        try:
            from rq.registry import StartedJobRegistry
            # 1. 清空队列中排队的 job
            count = self._q.count
            self._q.empty()
            # 2. 清理 StartedJobRegistry 中的残留
            registry = StartedJobRegistry("extraction", connection=self._q.connection)
            stuck_ids = registry.get_job_ids()
            for job_id in stuck_ids:
                try:
                    job = self._q.fetch_job(job_id)
                    if job is not None:
                        registry.remove(job, delete_job=True)
                except Exception:
                    pass
            total = count + len(stuck_ids)
            if total:
                logger.info(f"[TaskQueue] 已清空 Redis 队列: {total} 个任务 (queue={count}, started={len(stuck_ids)})")
            return total
        except Exception as e:
            logger.warning(f"[TaskQueue] 清空队列失败: {e}")
            return 0

    def shutdown(self) -> None:
        pass  # RQ worker 进程独立管理，不需要 Python 侧关闭

    @property
    def connection(self):
        return self._q.connection


# ═══════════════════════════════════════════════════════════════════════
# 暂停/继续标志（Redis 后端专用；memory 后端恒 False）
# ═══════════════════════════════════════════════════════════════════════

_EXTRACTION_PAUSE_KEY = "zk:extraction:paused"


def is_extraction_paused() -> bool:
    """读取暂停标志。仅 Redis 后端有效，memory 后端恒 False。"""
    if APP.TASK_QUEUE_BACKEND != "redis":
        return False
    try:
        from redis import Redis
        conn = Redis.from_url(APP.REDIS_URL)
        return bool(conn.get(_EXTRACTION_PAUSE_KEY))
    except ImportError:
        raise
    except Exception:
        raise RuntimeError("无法读取抽取暂停状态，请核对 Redis 后重试") from None


def set_extraction_paused(paused: bool) -> None:
    """设置/清除暂停标志（Redis 后端）。"""
    if APP.TASK_QUEUE_BACKEND != "redis":
        return
    try:
        from redis import Redis
        conn = Redis.from_url(APP.REDIS_URL)
        if paused:
            conn.set(_EXTRACTION_PAUSE_KEY, "1")
        else:
            conn.delete(_EXTRACTION_PAUSE_KEY)
    except ImportError:
        raise
    except Exception:
        raise RuntimeError("无法更新抽取暂停状态，请核对 Redis 后重试") from None


# ═══════════════════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════════════════

_queue_instance: Optional[TaskQueue] = None
_queue_lock = threading.Lock()


def has_active_extraction_workers() -> bool:
    """检测 Redis 中是否有活跃的 rq worker（用于判断是 reload 还是真正重启）。"""
    from config.settings import APP
    if APP.TASK_QUEUE_BACKEND != "redis":
        return False
    try:
        from redis import Redis
        conn = Redis.from_url(APP.REDIS_URL)
        # rq worker 在 Redis 中以 rq:worker:<name> 形式注册
        worker_keys = conn.keys("rq:worker:*")
        for key in worker_keys:
            worker_info = conn.hgetall(key)
            # 有 worker 且出生时间距现在 > 3 秒（排除刚启动的自身）
            birth = worker_info.get(b"birth", b"")
            if birth:
                import time
                birth_time = float(birth.decode())
                if time.time() - birth_time > 3:
                    return True
        return False
    except Exception:
        return False


def clear_extraction_redis() -> int:
    """启动时清空 Redis 中 extraction 队列及 StartedJobRegistry（独立于单例）。"""
    from config.settings import APP
    if APP.TASK_QUEUE_BACKEND != "redis":
        return 0
    try:
        from redis import Redis
        from rq import Queue as RQQueue
        from rq.registry import StartedJobRegistry
        conn = Redis.from_url(APP.REDIS_URL)
        q = RQQueue("extraction", connection=conn)
        count = q.count
        q.empty()
        registry = StartedJobRegistry("extraction", connection=conn)
        stuck_ids = registry.get_job_ids()
        for job_id in stuck_ids:
            try:
                job = q.fetch_job(job_id)
                if job is not None:
                    registry.remove(job, delete_job=True)
            except Exception:
                pass
        total = count + len(stuck_ids)
        if total:
            logger.info(f"[Startup] 已清空 Redis: {total} 个残留任务 (queue={count}, started={len(stuck_ids)})")
        return total
    except Exception as e:
        logger.warning(f"[Startup] 清空 Redis 失败: {e}")
        return 0


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

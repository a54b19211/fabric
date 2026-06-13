"""
LLM 抽取 Worker 管理器

模式：
  TASK_QUEUE_BACKEND=memory → 内存队列 + 线程 Worker（默认，重启丢任务）
  TASK_QUEUE_BACKEND=redis  → Redis 队列 + RQ Worker（持久化，重启不丢）

用法：
  manager = get_extraction_manager()          # 获取策略实例
  manager.submit(task)                        # 提交任务
  manager.stats()                             # 查看队列状态
  manager.shutdown()                          # 优雅关闭
"""

import threading
import queue
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from loguru import logger

from config.settings import APP


# ═══════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ExtractionTask:
    """单篇文档的抽取任务。"""
    uuid: str
    submit_time: float = field(default_factory=time.time)


_SENTINEL = None  # MemoryWorker 退出哨兵


# ═══════════════════════════════════════════════════════════════════════
# MemoryWorker + MemoryWorkerManager（backend=memory 时使用）
# ═══════════════════════════════════════════════════════════════════════

class MemoryWorker:
    """持有 queue.Queue 的线程 Worker，与原有行为一致。"""

    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self._queue: queue.Queue[Optional[ExtractionTask]] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=f"ExtractionWorker-{worker_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[Worker-{worker_id}] 已启动 (memory)")

    def enqueue(self, task: ExtractionTask) -> None:
        self._queue.put(task)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def shutdown(self) -> None:
        self._queue.put(_SENTINEL)
        self._thread.join()
        logger.info(f"[Worker-{self.worker_id}] 已关闭")

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task is _SENTINEL:
                self._queue.task_done()
                break
            self._process(task)
            self._queue.task_done()

    def _process(self, task: ExtractionTask) -> None:
        from flows.extraction_flow import extract_single_document
        from db.session import get_session
        from db.models import Document
        from db.repository import update_extraction_status

        logger.info(
            f"[Worker-{self.worker_id}] 开始处理: {task.uuid} "
            f"等待时间={time.time() - task.submit_time:.1f}s"
        )

        # 标记为正在执行（从 queued → processing）
        update_extraction_status(task.uuid, "processing")

        try:
            with get_session() as session:
                if session is None:
                    update_extraction_status(task.uuid, "failed", error="DB未连接")
                    return
                doc = session.query(Document).filter(
                    Document.uuid == task.uuid,
                    Document.is_deleted == False,
                ).first()
                if doc is None:
                    update_extraction_status(task.uuid, "failed", error="文档不存在")
                    return
                raw_content = doc.raw_content or ""
                source_site = doc.source_site or ""
                doc_type = doc.doc_type or ""

            if not raw_content.strip():
                update_extraction_status(task.uuid, "failed", error="文档raw_content为空")
                return

            success = extract_single_document(
                doc_uuid=task.uuid,
                raw_content=raw_content,
                source_site=source_site,
                doc_type=doc_type,
            )
            if not success:
                update_extraction_status(task.uuid, "failed", error="抽取返回失败")
        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] 处理异常: {task.uuid} | {e}")
            try:
                update_extraction_status(task.uuid, "failed", error=str(e))
            except Exception:
                pass

        logger.info(
            f"[Worker-{self.worker_id}] 处理完成: {task.uuid} "
            f"success={task.uuid}"
        )


class MemoryWorkerManager:
    """管理 N 个 MemoryWorker，FCFS 分配到最短队列。"""

    def __init__(self, num_workers: int = 3):
        if num_workers < 1:
            raise ValueError(f"num_workers 必须 >= 1，当前: {num_workers}")
        self._workers = [MemoryWorker(i) for i in range(num_workers)]
        self._lock = threading.Lock()
        self._num_workers = num_workers
        logger.info(f"[WorkerManager] Memory 模式，Worker 数量: {num_workers}")

    def submit(self, task: ExtractionTask) -> int:
        with self._lock:
            selected = min(self._workers, key=lambda w: (w.queue_size, w.worker_id))
            selected.enqueue(task)
            logger.debug(
                f"[WorkerManager] 任务分配: {task.uuid} → Worker-{selected.worker_id} "
                f"(队列长度: {selected.queue_size})"
            )
            return selected.worker_id

    @property
    def num_workers(self) -> int:
        return self._num_workers

    def total_pending(self) -> int:
        return sum(w.queue_size for w in self._workers)

    def stats(self) -> list[dict]:
        return [
            {"worker_id": w.worker_id, "queue_size": w.queue_size}
            for w in self._workers
        ]

    def shutdown(self) -> None:
        logger.info("[WorkerManager] 开始关闭...")
        for w in self._workers:
            w.shutdown()
        logger.info("[WorkerManager] 所有 Worker 已关闭")


# ═══════════════════════════════════════════════════════════════════════
# RedisWorkerManager（backend=redis 时使用）
# ═══════════════════════════════════════════════════════════════════════

class RedisWorkerManager:
    """通过 RQ 将任务推入 Redis，由独立 rq worker 进程消费。"""

    def __init__(self):
        from core_engine.task_queue import get_task_queue
        self._tq = get_task_queue()
        self._num_workers = APP.EXTRACTION_CONCURRENCY
        # Redis 层恢复：重入队 StartedJobRegistry 中丢失的 job
        stuck = self._tq.recover_pending()
        if stuck:
            logger.info(f"[WorkerManager] 恢复 {len(stuck)} 个未完成任务")
        logger.info(f"[WorkerManager] Redis 模式，需启动 {self._num_workers} 个 rq worker 进程")

    def submit(self, task: ExtractionTask) -> int:
        from core_engine.task_queue import Task as TQTask
        self._tq.enqueue(TQTask(uuid=task.uuid))
        return 0

    @property
    def num_workers(self) -> int:
        return self._num_workers

    def total_pending(self) -> int:
        return self._tq.size()

    def stats(self) -> list[dict]:
        return [{"worker_id": 0, "queue_size": self._tq.size()}]

    def shutdown(self) -> None:
        pass  # RQ worker 进程独立管理


# ═══════════════════════════════════════════════════════════════════════
# 全局单例工厂
# ═══════════════════════════════════════════════════════════════════════

_manager_instance = None
_manager_lock = threading.Lock()


def get_extraction_manager():
    """根据 APP.TASK_QUEUE_BACKEND 返回对应的 Manager（单例）。"""
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                if APP.TASK_QUEUE_BACKEND == "redis":
                    _manager_instance = RedisWorkerManager()
                else:
                    _manager_instance = MemoryWorkerManager(
                        num_workers=APP.EXTRACTION_CONCURRENCY
                    )
    return _manager_instance

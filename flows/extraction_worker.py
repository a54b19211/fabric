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

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

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

    def __init__(self, worker_id: int, pause_event: threading.Event):
        self.worker_id = worker_id
        self._pause_event = pause_event
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
            self._pause_event.wait()  # 暂停时阻塞在这里，不再消费队列
            task = self._queue.get()
            if task is _SENTINEL:
                self._queue.task_done()
                break
            self._process(task)
            self._queue.task_done()

    def _process(self, task: ExtractionTask) -> None:
        from db.repository import update_extraction_status
        from flows.extraction_flow import extract_single_document

        logger.info(
            f"[Worker-{self.worker_id}] 开始处理: {task.uuid} "
            f"等待时间={time.time() - task.submit_time:.1f}s"
        )

        # 标记为正在执行（从 queued → processing）
        update_extraction_status(task.uuid, "processing")

        try:
            from db.extraction_subjects import load_subject

            subject = load_subject(task.uuid)
            if subject is None:
                update_extraction_status(task.uuid, "failed", error="文档不存在")
                return
            raw_content, source_site, doc_type, is_history = subject

            if not raw_content.strip():
                update_extraction_status(task.uuid, "failed", error="文档raw_content为空")
                return

            success = extract_single_document(
                doc_uuid=task.uuid,
                raw_content=raw_content,
                source_site=source_site,
                doc_type=doc_type,
                native_allowed=not is_history,
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
        self._pause_event = threading.Event()
        self._pause_event.set()  # 默认继续（未暂停）
        self._workers = [MemoryWorker(i, self._pause_event) for i in range(num_workers)]
        self._lock = threading.Lock()
        self._num_workers = num_workers
        logger.info(f"[WorkerManager] Memory 模式，Worker 数量: {num_workers}")

    def pause(self) -> None:
        """暂停抽取：Worker 完成当前任务后停止消费队列。"""
        self._pause_event.clear()
        logger.info("[WorkerManager] 已暂停抽取")

    def resume(self) -> None:
        """继续抽取：恢复 Worker 消费队列。"""
        self._pause_event.set()
        logger.info("[WorkerManager] 已继续抽取")

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

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
        self._pause_event.set()  # 先恢复，确保 Worker 能退出阻塞
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
        # 查询状态也会创建管理对象；不能据此认定在途任务已失活。
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

    def pause(self) -> None:
        """暂停抽取：rq worker 处理任务前检查标志，暂停时等待。"""
        from core_engine.task_queue import set_extraction_paused
        set_extraction_paused(True)
        logger.info("[WorkerManager] 已暂停抽取 (redis)")

    def resume(self) -> None:
        """继续抽取：清除暂停标志。"""
        from core_engine.task_queue import set_extraction_paused
        set_extraction_paused(False)
        logger.info("[WorkerManager] 已继续抽取 (redis)")

    def is_paused(self) -> bool:
        from core_engine.task_queue import is_extraction_paused
        return is_extraction_paused()

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

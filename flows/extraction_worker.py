"""
LLM 抽取 Worker 管理器

架构：
  ExtractionWorkerManager
    ├── Worker-0: queue.Queue → 串行处理每篇文档（含所有块）
    ├── Worker-1: queue.Queue → 串行处理每篇文档（含所有块）
    └── Worker-N: queue.Queue → 串行处理每篇文档（含所有块）

调度算法：FCFS（先来先服务）
  - 新任务到来时，选择当前队列长度最短的 Worker
  - 队列长度相同时，选择 Worker ID 最小的（稳定性）

任务粒度：文档（一篇文档的所有块在同一 Worker 内串行处理）
  - 保证同文档块间的记忆上下文依赖
  - 不同文档并行，向 vLLM 发出 N 个并发请求

回调：
  - 任务完成后调用 on_done(uuid, success, error)
  - 调用方可用回调更新状态、触发下游流程

使用方式：
  manager = get_extraction_manager()          # 获取全局单例
  manager.submit(doc_task, on_done=callback)  # 提交任务
  manager.stats()                             # 查看各 Worker 队列状态
  manager.shutdown()                          # 优雅关闭（等待所有任务完成）
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
    """单篇文档的抽取任务"""
    uuid: str
    raw_content: str
    source_site: str
    doc_type: str
    submit_time: float = field(default_factory=time.time)
    on_done: Optional[Callable[[str, bool, str], None]] = None
    # on_done(uuid, success, error_msg)


_SENTINEL = None  # 用于通知 Worker 退出的哨兵值


# ═══════════════════════════════════════════════════════════════════════
# Worker
# ═══════════════════════════════════════════════════════════════════════

class ExtractionWorker:
    """
    单个 Worker，持有一个任务队列，在独立线程中串行处理文档。
    """

    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self._queue: queue.Queue[Optional[ExtractionTask]] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=f"ExtractionWorker-{worker_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"[Worker-{worker_id}] 已启动")

    # ── 公开接口 ──────────────────────────────────────────────────────

    def enqueue(self, task: ExtractionTask) -> None:
        """将任务放入队列。"""
        self._queue.put(task)
        logger.debug(
            f"[Worker-{self.worker_id}] 任务入队: {task.uuid} "
            f"(队列长度: {self.queue_size})"
        )

    @property
    def queue_size(self) -> int:
        """当前队列中待处理的任务数（不含正在处理的）。"""
        return self._queue.qsize()

    def shutdown(self) -> None:
        """发送退出信号，等待当前任务处理完毕后退出。"""
        self._queue.put(_SENTINEL)
        self._thread.join()
        logger.info(f"[Worker-{self.worker_id}] 已关闭")

    # ── 内部循环 ──────────────────────────────────────────────────────

    def _run(self) -> None:
        """Worker 主循环：持续从队列取任务并处理。"""
        while True:
            task = self._queue.get()

            # 收到哨兵，退出
            if task is _SENTINEL:
                self._queue.task_done()
                break

            self._process(task)
            self._queue.task_done()

    def _process(self, task: ExtractionTask) -> None:
        """处理单篇文档，完成后触发回调。"""
        from flows.extraction_flow import extract_single_document

        logger.info(
            f"[Worker-{self.worker_id}] 开始处理: {task.uuid} "
            f"site={task.source_site} 等待时间={time.time() - task.submit_time:.1f}s"
        )

        success = False
        error_msg = ""
        try:
            success = extract_single_document(
                doc_uuid=task.uuid,
                raw_content=task.raw_content,
                source_site=task.source_site,
                doc_type=task.doc_type,
            )
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Worker-{self.worker_id}] 处理异常: {task.uuid} | {e}")

        # 触发回调
        if task.on_done is not None:
            try:
                task.on_done(task.uuid, success, error_msg)
            except Exception as e:
                logger.error(f"[Worker-{self.worker_id}] 回调异常: {task.uuid} | {e}")

        logger.info(
            f"[Worker-{self.worker_id}] 处理完成: {task.uuid} "
            f"success={success}"
        )


# ═══════════════════════════════════════════════════════════════════════
# WorkerManager
# ═══════════════════════════════════════════════════════════════════════

class ExtractionWorkerManager:
    """
    管理 N 个 Worker，使用 FCFS 算法将任务分配到最短队列。

    FCFS（先来先服务）实现：
      - 选择 queue_size 最小的 Worker
      - 相同时选 worker_id 最小的（保证稳定性）
    """

    def __init__(self, num_workers: int):
        if num_workers < 1:
            raise ValueError(f"num_workers 必须 >= 1，当前: {num_workers}")
        self._workers = [ExtractionWorker(i) for i in range(num_workers)]
        self._lock = threading.Lock()
        logger.info(f"[WorkerManager] 初始化完成，Worker 数量: {num_workers}")

    # ── 公开接口 ──────────────────────────────────────────────────────

    def submit(
        self,
        task: ExtractionTask,
    ) -> int:
        """
        提交任务，FCFS 选择最短队列的 Worker。
        返回被选中的 worker_id。
        """
        with self._lock:
            # FCFS：选队列最短的 Worker（相同时选 id 最小的）
            selected = min(self._workers, key=lambda w: (w.queue_size, w.worker_id))
            selected.enqueue(task)
            logger.info(
                f"[WorkerManager] 任务分配: {task.uuid} → Worker-{selected.worker_id} "
                f"(队列长度: {selected.queue_size})"
            )
            return selected.worker_id

    def stats(self) -> list[dict]:
        """返回各 Worker 的队列状态快照。"""
        return [
            {
                "worker_id": w.worker_id,
                "queue_size": w.queue_size,
            }
            for w in self._workers
        ]

    def total_pending(self) -> int:
        """所有 Worker 队列中待处理的任务总数。"""
        return sum(w.queue_size for w in self._workers)

    def shutdown(self, wait: bool = True) -> None:
        """
        优雅关闭所有 Worker。
        wait=True 时等待所有任务处理完毕再退出。
        """
        logger.info("[WorkerManager] 开始关闭...")
        for w in self._workers:
            w.shutdown()
        logger.info("[WorkerManager] 所有 Worker 已关闭")

    @property
    def num_workers(self) -> int:
        return len(self._workers)


# ═══════════════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════════════

_manager_instance: Optional[ExtractionWorkerManager] = None
_manager_lock = threading.Lock()


def get_extraction_manager() -> ExtractionWorkerManager:
    """
    获取全局 WorkerManager 单例。
    首次调用时根据 APP.EXTRACTION_CONCURRENCY 初始化。
    线程安全。
    """
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                n = APP.EXTRACTION_CONCURRENCY
                logger.info(f"[WorkerManager] 创建全局单例，Worker 数量: {n}")
                _manager_instance = ExtractionWorkerManager(num_workers=n)
    return _manager_instance

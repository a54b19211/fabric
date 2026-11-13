# -*- coding: utf-8 -*-
"""实体合并消歧内存队列 — 后台串行消费，避免 LLM 调用阻塞 API。"""

import queue
import threading

from loguru import logger


class EntityMergeQueue:
    """内存队列：维护待消歧实体，后台线程串行消费。"""

    def __init__(self):
        self._queue: queue.Queue[int] = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._completed = 0
        self._failed = 0
        self._total = 0

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._worker = threading.Thread(target=self._process, daemon=True)
            self._worker.start()
            logger.info("[MergeQueue] 后台工作线程已启动")

    def stop(self):
        with self._lock:
            self._running = False
            logger.info("[MergeQueue] 收到停止信号")

    def enqueue(self, entity_ids: list[int]) -> int:
        with self._lock:
            for eid in entity_ids:
                self._queue.put(eid)
            self._total += len(entity_ids)
            return self._queue.qsize()

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "pending": self._queue.qsize(),
                "completed": self._completed,
                "failed": self._failed,
                "total": self._total,
            }

    def _process(self):
        from graph.agents.runner import get_agent_runner

        runner = get_agent_runner()
        logger.info("[MergeQueue] 工作线程开始消费")

        while True:
            with self._lock:
                if not self._running:
                    break

            try:
                entity_id = self._queue.get(timeout=2)
            except queue.Empty:
                continue

            try:
                logger.info(f"[MergeQueue] 开始消歧 entity_id={entity_id}")
                result = runner.run_entity_merge(entity_id)
                decision = result.get("decision", "error")
                with self._lock:
                    if decision == "error":
                        self._failed += 1
                    else:
                        self._completed += 1
                logger.info(
                    f"[MergeQueue] entity_id={entity_id} 完成: decision={decision}, "
                    f"queue_pending={self._queue.qsize()}"
                )
            except Exception as e:
                with self._lock:
                    self._failed += 1
                logger.error(f"[MergeQueue] entity_id={entity_id} 异常: {e}")


# 模块级单例
_merge_queue: EntityMergeQueue | None = None


def get_merge_queue() -> EntityMergeQueue:
    global _merge_queue
    if _merge_queue is None:
        _merge_queue = EntityMergeQueue()
        _merge_queue.start()
    return _merge_queue

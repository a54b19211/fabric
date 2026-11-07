"""RQ 任务入口 —— Redis 队列模式下 Worker 执行的实际工作单元。"""
import time

from loguru import logger


def extraction_job(uuid: str) -> bool:
    """
    RQ job 函数：从 DB 取文档内容 → 调用 extract_single_document。
    任务内部自行更新 extraction_status，无需回调。
    返回 True/False 表示成功/失败，RQ 会记录 job result。
    """
    from core_engine.task_queue import is_extraction_paused
    from db.extraction_subjects import load_subject
    from db.repository import update_extraction_status
    from flows.extraction_flow import extract_single_document

    # 暂停时等待，直到恢复（不标记 processing，任务保持排队状态）
    try:
        while is_extraction_paused():
            time.sleep(2)
    except Exception:
        # 读取失败时不开始抽取，并让 RQ 保留失败记录。
        try:
            update_extraction_status(
                uuid, "failed", error="无法读取暂停状态，抽取尚未开始，请核对后重试",
            )
        except Exception:
            logger.error("[RQ-Job] 暂停状态读取失败，且未能保存任务失败状态")
        raise

    logger.info(f"[RQ-Job] 开始: {uuid}")

    # 标记为正在执行（从 queued → processing）
    update_extraction_status(uuid, "processing")

    try:
        subject = load_subject(uuid)
        if subject is None:
            update_extraction_status(uuid, "failed", error="文档不存在")
            return False
        raw_content, source_site, doc_type, is_history = subject

        if not raw_content.strip():
            update_extraction_status(uuid, "failed", error="文档raw_content为空")
            return False

        success = extract_single_document(
            doc_uuid=uuid,
            raw_content=raw_content,
            source_site=source_site,
            doc_type=doc_type,
            native_allowed=not is_history,
        )
        logger.info(f"[RQ-Job] 完成: {uuid} success={success}")
        return success

    except Exception as e:
        logger.error(f"[RQ-Job] 异常: {uuid} | {e}")
        try:
            update_extraction_status(uuid, "failed", error=str(e))
        except Exception:
            logger.error(f"[RQ-Job] 更新状态失败: {uuid}")
        return False

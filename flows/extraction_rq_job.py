"""RQ 任务入口 —— Redis 队列模式下 Worker 执行的实际工作单元。"""
from loguru import logger


def extraction_job(uuid: str) -> bool:
    """
    RQ job 函数：从 DB 取文档内容 → 调用 extract_single_document。
    任务内部自行更新 extraction_status，无需回调。
    返回 True/False 表示成功/失败，RQ 会记录 job result。
    """
    from db.session import get_session
    from db.models import Document
    from db.repository import update_extraction_status
    from flows.extraction_flow import extract_single_document

    logger.info(f"[RQ-Job] 开始: {uuid}")

    # 标记为正在执行（从 queued → processing）
    update_extraction_status(uuid, "processing")

    try:
        with get_session() as session:
            if session is None:
                update_extraction_status(uuid, "failed", error="DB未连接")
                return False
            doc = session.query(Document).filter(
                Document.uuid == uuid,
                Document.is_deleted == False,
            ).first()
            if doc is None:
                update_extraction_status(uuid, "failed", error="文档不存在")
                return False
            raw_content = doc.raw_content or ""
            source_site = doc.source_site or ""
            doc_type = doc.doc_type or ""

        if not raw_content.strip():
            update_extraction_status(uuid, "failed", error="文档raw_content为空")
            return False

        success = extract_single_document(
            doc_uuid=uuid,
            raw_content=raw_content,
            source_site=source_site,
            doc_type=doc_type,
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

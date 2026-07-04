# -*- coding: utf-8 -*-
"""审核生命周期管理 — 实体/关系变更后自动维护 document 级缓存计数。"""

from loguru import logger
from sqlalchemy import text


def recalc_document_state(session, doc_uuid: str) -> None:
    """重新计算文档的 pending_entity_count + total_entity_count。
    在实体插入、审核状态变更、实体删除后调用。
    """
    if not doc_uuid:
        return
    try:
        session.execute(
            text("""
                UPDATE documents SET
                    pending_entity_count = (
                        SELECT COUNT(*) FROM extracted_entities
                        WHERE source_doc_uuid = :uuid
                          AND review_status = 'pending'
                          AND is_deleted = false
                    ),
                    total_entity_count = (
                        SELECT COUNT(*) FROM extracted_entities
                        WHERE source_doc_uuid = :uuid
                          AND is_deleted = false
                    )
                WHERE uuid = :uuid
            """),
            {"uuid": doc_uuid},
        )
    except Exception as e:
        logger.error(f"[review_lifecycle] 重算文档状态失败: doc_uuid={doc_uuid} | {e}")

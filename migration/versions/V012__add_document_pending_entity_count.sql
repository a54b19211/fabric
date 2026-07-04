-- V012: 给 documents 表加实体计数列，由 review_lifecycle 模块自动维护
ALTER TABLE documents ADD COLUMN IF NOT EXISTS pending_entity_count INT DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS total_entity_count INT DEFAULT 0;

-- 存量数据回填
UPDATE documents d SET
    pending_entity_count = (
        SELECT COUNT(*) FROM extracted_entities e
        WHERE e.source_doc_uuid = d.uuid
          AND e.review_status = 'pending'
          AND e.is_deleted = false
    ),
    total_entity_count = (
        SELECT COUNT(*) FROM extracted_entities e
        WHERE e.source_doc_uuid = d.uuid
          AND e.is_deleted = false
    );

CREATE INDEX IF NOT EXISTS idx_documents_pending_entity_count
    ON documents(pending_entity_count)
    WHERE pending_entity_count > 0;

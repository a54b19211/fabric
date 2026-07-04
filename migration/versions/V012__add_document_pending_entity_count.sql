-- V012: 给 documents 表加实体计数列，由 review_lifecycle 模块自动维护
ALTER TABLE documents ADD COLUMN IF NOT EXISTS pending_entity_count INT DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS total_entity_count INT DEFAULT 0;

-- 存量数据回填（GROUP BY + JOIN，单次扫描 extracted_entities，避免关联子查询）
UPDATE documents d SET
    pending_entity_count = COALESCE(stats.pc, 0),
    total_entity_count   = COALESCE(stats.tc, 0)
FROM (
    SELECT source_doc_uuid,
           COUNT(*) FILTER (WHERE review_status = 'pending' AND is_deleted = false) AS pc,
           COUNT(*) FILTER (WHERE is_deleted = false) AS tc
    FROM extracted_entities
    GROUP BY source_doc_uuid
) stats
WHERE d.uuid = stats.source_doc_uuid;

CREATE INDEX IF NOT EXISTS idx_documents_pending_entity_count
    ON documents(pending_entity_count)
    WHERE pending_entity_count > 0;

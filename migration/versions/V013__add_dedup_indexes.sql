-- ============================================================
-- V013 — extracted_entities / entity_relations 去重索引
-- 防止同一文档产生完全相同的实体和关系
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS idx_ent_doc_name_type
    ON extracted_entities(source_doc_uuid, entity_name, entity_type, is_deleted)
    WHERE is_deleted = FALSE;

CREATE INDEX IF NOT EXISTS idx_rel_dedup
    ON entity_relations(source_doc_uuid, from_entity_id, to_entity_id, relation_type)
    WHERE is_deleted = FALSE;

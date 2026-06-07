-- ============================================================
-- V010 — 复合索引优化
-- 为高频查询添加组合索引，加速审核列表和推送队列查询
-- ============================================================

-- extracted_entities: 审核队列高频查询 (review_status + is_deleted)
CREATE INDEX IF NOT EXISTS idx_ent_review_deleted
    ON extracted_entities(review_status, is_deleted);

-- extracted_entities: WeKnora 图推送队列查询 (weknora_graph_status + review_status + is_deleted)
CREATE INDEX IF NOT EXISTS idx_ent_weknora_review
    ON extracted_entities(weknora_graph_status, review_status, is_deleted);

-- entity_relations: 审核队列高频查询
CREATE INDEX IF NOT EXISTS idx_rel_review_deleted
    ON entity_relations(review_status, is_deleted);

-- entity_relations: WeKnora 图推送队列查询
CREATE INDEX IF NOT EXISTS idx_rel_weknora_review
    ON entity_relations(weknora_graph_status, review_status, is_deleted);

-- documents: 抽取队列查询 (extraction_status + is_deleted)
CREATE INDEX IF NOT EXISTS idx_doc_extract_deleted
    ON documents(extraction_status, is_deleted);

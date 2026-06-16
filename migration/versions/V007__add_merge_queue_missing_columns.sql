-- V007 — entity_merge_queue 补漏字段
-- V004 创建表时遗漏了 existing_source_entity_id 和 existing_source_doc_uuid，
-- 导致 repository 写入时报 column does not exist 错误。

ALTER TABLE entity_merge_queue
    ADD COLUMN IF NOT EXISTS existing_source_entity_id BIGINT;

ALTER TABLE entity_merge_queue
    ADD COLUMN IF NOT EXISTS existing_source_doc_uuid TEXT DEFAULT '';

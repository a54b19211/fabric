-- V005 — WeKnora 图同步状态追踪字段
-- 实体字典同步状态、抽取实体/关系的图同步状态

-- entity_dict: 追踪是否已推送到 WeKnora 镜像表
ALTER TABLE entity_dict
    ADD COLUMN IF NOT EXISTS weknora_sync_status TEXT DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_dict_weknora_sync ON entity_dict(weknora_sync_status);

-- extracted_entities: 追踪是否已推送到 WeKnora graph_entities
ALTER TABLE extracted_entities
    ADD COLUMN IF NOT EXISTS weknora_graph_status TEXT DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_ent_weknora_graph ON extracted_entities(weknora_graph_status);

-- entity_relations: 追踪是否已推送到 WeKnora graph_relations
ALTER TABLE entity_relations
    ADD COLUMN IF NOT EXISTS weknora_graph_status TEXT DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_rel_weknora_graph ON entity_relations(weknora_graph_status);

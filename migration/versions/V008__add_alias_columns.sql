-- V008 — entity_aliases 补全 entity_type / created_at / updated_at
BEGIN;

ALTER TABLE entity_aliases
    ADD COLUMN IF NOT EXISTS entity_type TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS created_at   TIMESTAMPTZ DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ DEFAULT now();

-- 旧数据回填 entity_type
UPDATE entity_aliases
SET entity_type = entity_dict.entity_type
FROM entity_dict
WHERE entity_aliases.dict_id = entity_dict.id
  AND entity_aliases.entity_type = '';

-- 跨 dict_id 去重索引（用于写入时检查冲突）
CREATE INDEX IF NOT EXISTS idx_alias_entity_type ON entity_aliases(entity_type);

COMMIT;

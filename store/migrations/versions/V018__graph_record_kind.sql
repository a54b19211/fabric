-- 仅放宽既有版本类型；原件/旧哈希/表和队列不变。
ALTER TABLE history_versions DROP CONSTRAINT IF EXISTS history_versions_record_kind_check;
ALTER TABLE history_versions ADD CONSTRAINT history_versions_record_kind_check CHECK (record_kind IN ('trial','program','outcome','document','graph'));

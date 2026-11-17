-- 2026-11-17：知识库从一个大库（fabric-kdd，定长分块）改成按资料类型分的十个库；采集全部开工。
-- V051 留给 /data/projects/pending 里那份删旧质量表的脚本，这里不动它。

-- 1. 打开要进知识库的来源的可读开关（V047 默认关上的那批）。NCCN、本地导入、SEC 公司概念事实不动，仍关着。
WITH opened AS (
  UPDATE fabric_sources SET read_enabled = true
  WHERE NOT read_enabled AND source_id IN (
    'cde_zdyz', 'fda_drug_safety', 'fda_press', 'fiercepharma', 'gdelt_doc', 'hkex',
    'phirda_industry', 'phirda_policy', 'phirda_rd', 'phirda_healthcare_reform', 'pubmed', 'roche')
  RETURNING source_id, read_enabled, fetch_enabled)
INSERT INTO fabric_source_switch_log (source_id, changed_by, read_enabled, fetch_enabled)
SELECT source_id, 'migration V052', read_enabled, fetch_enabled FROM opened;

-- 2. 退役来源（fetch/catalog.py · RETIRED）不再定时抓；已存的旧版本照读，可读开关不动。
WITH retired AS (
  UPDATE fabric_sources SET fetch_enabled = false
  WHERE fetch_enabled AND source_id IN ('legacy_documents', 'reviewed_graph', 'primekg_graph')
  RETURNING source_id, read_enabled, fetch_enabled)
INSERT INTO fabric_source_switch_log (source_id, changed_by, read_enabled, fetch_enabled)
SELECT source_id, 'migration V052', read_enabled, fetch_enabled FROM retired;

-- 3. 旧知识库 fabric-kdd 已在 WeKnora 里整库删除（主持人 2026-11-17 批准）：指向它的索引待办和索引目标一起清掉，
--    不再一条一条请桥接撤下。别的库里没有这个知识库编号，这两句什么也不删。
DELETE FROM index_publications
WHERE target_id IN (SELECT id FROM index_targets
                    WHERE config->>'knowledge_base_id' = 'fa1be120-adad-4080-a9bc-8b8ab76e7af4');
DELETE FROM index_targets WHERE config->>'knowledge_base_id' = 'fa1be120-adad-4080-a9bc-8b8ab76e7af4';

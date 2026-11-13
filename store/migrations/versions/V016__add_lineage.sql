-- V016: documents 表新增 lineage 字段级血缘列
-- 背景：字段级血缘追踪能力，记录 title/drug_name/approval_number/manufacturer
-- 在各加工步骤（parse/clean/normalize/quality）中的输入输出值。
-- 对应 db/models.py Document.lineage 字段。

ALTER TABLE documents ADD COLUMN IF NOT EXISTS lineage TEXT DEFAULT '';

-- 2026-11-18：Fabric 只抓、洗、存，不记“服务哪类业务判断”。目录不再带 decisions，画像不再写这一列。
-- 列里是目录抄来的字母（A–E），没有别处读它；整列删掉（只改元数据，不重写行）。
ALTER TABLE derived_profiles DROP COLUMN IF EXISTS decisions;

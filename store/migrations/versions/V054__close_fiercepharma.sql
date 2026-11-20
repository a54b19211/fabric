-- 2026-11-20：关掉 Fierce Pharma（主持人批准）。文章页每次 403（Cloudflare 人机验证），不绕过；RSS 只有标题和摘要，
-- 每轮 20 次请求、0 条。可读开关和定时抓取开关都关；已存的旧版本留着，开关打开就能再读。
WITH closed AS (
  UPDATE fabric_sources SET read_enabled = false, fetch_enabled = false
  WHERE (read_enabled OR fetch_enabled) AND source_id = 'fiercepharma'
  RETURNING source_id, read_enabled, fetch_enabled)
INSERT INTO fabric_source_switch_log (source_id, changed_by, read_enabled, fetch_enabled)
SELECT source_id, 'migration V054', read_enabled, fetch_enabled FROM closed;

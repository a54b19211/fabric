-- 删掉测量子系统（V037–V041 建的表）。2026-11-16 主人决定：从 WHO DON 公告里抽病例数存成序列是过度设计，
-- 线上从没跑过（这些表只在重建的新库里有），只服务一个来源、78 条公告。程序已整个删除；
-- 以后要用公告里的数字，由分析时的模型引用清洗正文里的原句，BioCheck 按原文核对。V037–V041 已在新库执行，不改它们。
DROP TABLE IF EXISTS
  derived_measure_correction_resolutions,
  derived_measure_correction_claims,
  derived_measure_correction_cursor,
  derived_measure_correction_links,
  derived_measurement_identities,
  derived_measurement_events,
  derived_measurement_feed,
  derived_measurements,
  derived_measure_relocations,
  derived_measure_readings,
  derived_measure_requests,
  derived_measure_window_attempts,
  derived_measure_windows,
  derived_measure_runs;
DROP FUNCTION IF EXISTS derived_measure_requests_settle_once();
DROP FUNCTION IF EXISTS derived_measure_append_only();

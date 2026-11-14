-- 主库统一叫“资料库”（英文 fabric）：history_* 表改名 fabric_*，表上以 history_ 开头的约束、索引、序列、规则、
-- 触发器和触发器函数一并改名。只改名字，数据不动。V017–V045 是已有的迁移文件（现役库执行过的不再改），里面的旧名不改。
-- 改名要拿表的排他锁：先停掉读写本库的服务（runner 每个文件 lock_timeout 3s，拿不到锁就整份回滚，什么都不改）。

ALTER TABLE history_sources RENAME TO fabric_sources;
ALTER TABLE history_runs RENAME TO fabric_runs;
ALTER TABLE history_versions RENAME TO fabric_versions;
ALTER TABLE history_version_rank RENAME TO fabric_version_rank;
ALTER TABLE history_attachment_links RENAME TO fabric_attachment_links;
ALTER TABLE history_source_lifecycle RENAME TO fabric_source_lifecycle;
ALTER TABLE history_source_watches RENAME TO fabric_source_watches;

-- 约束（主键、唯一键的索引随约束一起改名）、其余索引、序列、规则：名字开头的 history_ 换成 fabric_。按库里实际有的改：
-- 现役库是旧 runner 一路建起来的，个别名字可能和新建的库不同。（不用 format 和 LIKE：runner 经 psycopg2 执行，百分号会被
-- 当成参数占位符。）
DO $$
DECLARE o record;
BEGIN
  FOR o IN SELECT c.conrelid::regclass::text AS tbl, c.conname AS name FROM pg_constraint c
           JOIN pg_namespace n ON n.oid = c.connamespace
           WHERE n.nspname = current_schema() AND left(c.conname, 8) = 'history_' LOOP
    EXECUTE 'ALTER TABLE ' || o.tbl || ' RENAME CONSTRAINT ' || quote_ident(o.name)
            || ' TO ' || quote_ident('fabric_' || substr(o.name, 9));
  END LOOP;
  FOR o IN SELECT c.relname AS name, CASE c.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'INDEX' END AS kind
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = current_schema() AND c.relkind IN ('i', 'S') AND left(c.relname, 8) = 'history_' LOOP
    EXECUTE 'ALTER ' || o.kind || ' ' || quote_ident(o.name) || ' RENAME TO ' || quote_ident('fabric_' || substr(o.name, 9));
  END LOOP;
  FOR o IN SELECT r.ev_class::regclass::text AS tbl, r.rulename AS name FROM pg_rewrite r
           JOIN pg_class c ON c.oid = r.ev_class JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = current_schema() AND left(r.rulename, 8) = 'history_' LOOP
    EXECUTE 'ALTER RULE ' || quote_ident(o.name) || ' ON ' || o.tbl || ' RENAME TO '
            || quote_ident('fabric_' || substr(o.name, 9));
  END LOOP;
END $$;

-- 只追加的保护：触发器函数换成新名字（报错信息写新表名），触发器改挂新函数，旧函数删掉。
CREATE OR REPLACE FUNCTION fabric_versions_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.version_id, NEW.source_id, NEW.is_demo, NEW.source_record_id, NEW.record_kind, NEW.first_seen_at,
      NEW.received_at, NEW.proof_available_at, NEW.published_seq, NEW.payload)
     IS DISTINCT FROM
     (OLD.version_id, OLD.source_id, OLD.is_demo, OLD.source_record_id, OLD.record_kind, OLD.first_seen_at,
      OLD.received_at, OLD.proof_available_at, OLD.published_seq, OLD.payload) THEN
    RAISE EXCEPTION 'fabric_versions is append-only: only fingerprint and replaced_by may be filled in';
  END IF;
  IF (OLD.fingerprint IS NOT NULL AND NEW.fingerprint IS DISTINCT FROM OLD.fingerprint)
     OR (OLD.replaced_by IS NOT NULL AND NEW.replaced_by IS DISTINCT FROM OLD.replaced_by) THEN
    RAISE EXCEPTION 'fabric_versions: fingerprint and replaced_by are filled in once, never changed';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS history_versions_append_only ON fabric_versions;
CREATE TRIGGER fabric_versions_append_only BEFORE UPDATE ON fabric_versions
  FOR EACH ROW EXECUTE FUNCTION fabric_versions_append_only();
DROP FUNCTION IF EXISTS history_versions_append_only();

CREATE OR REPLACE FUNCTION fabric_source_lifecycle_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'fabric_source_lifecycle is append-only';
END
$$;
DROP TRIGGER IF EXISTS history_source_lifecycle_append_only ON fabric_source_lifecycle;
DROP TRIGGER IF EXISTS history_source_lifecycle_no_truncate ON fabric_source_lifecycle;
CREATE TRIGGER fabric_source_lifecycle_append_only BEFORE UPDATE OR DELETE ON fabric_source_lifecycle
 FOR EACH ROW EXECUTE FUNCTION fabric_source_lifecycle_append_only();
CREATE TRIGGER fabric_source_lifecycle_no_truncate BEFORE TRUNCATE ON fabric_source_lifecycle
 FOR EACH STATEMENT EXECUTE FUNCTION fabric_source_lifecycle_append_only();
DROP FUNCTION IF EXISTS history_source_lifecycle_append_only();

-- 迁移账本：本库以前的行记在 db_role='history' 下，改记成 'fabric'（store/migrations/runner.py 的 BEFORE_V046）。
UPDATE fabric_schema_ledger_events SET db_role = 'fabric' WHERE db_role = 'history';

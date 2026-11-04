-- 抽取/审核的主体（documents 行）可以是 history 的一个固定版本：行里只存版本引用和所用清洗正文的身份，
-- 不复制正文；抽取执行时按引用从 history 读该版本的清洗正文并核对哈希。
ALTER TABLE documents ADD COLUMN IF NOT EXISTS history_source_id text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS history_record_id text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS history_version_id char(64);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS history_text_version text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS history_text_sha256 char(64);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS history_is_demo boolean;
CREATE UNIQUE INDEX IF NOT EXISTS documents_history_version ON documents (history_version_id)
  WHERE history_version_id IS NOT NULL;

-- 这类主体的原件本来就在 history 里：不再冻结回 history（否则会绕一圈成为 legacy_documents 的新版本）。
-- 图（审核结果）的待办照常记录，由发表方按版本引用处理。
CREATE OR REPLACE FUNCTION publication_document_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE value jsonb;
BEGIN
 IF COALESCE(CASE WHEN TG_OP='DELETE' THEN OLD.history_version_id ELSE NEW.history_version_id END, '') <> '' THEN
  RETURN NULL;
 END IF;
 IF TG_OP='DELETE' THEN
  value := to_jsonb(OLD) || '{"is_deleted":true}'::jsonb;
  PERFORM publication_record_document(value,'source_deleted');
 ELSIF TG_OP='INSERT' THEN
  PERFORM publication_record_document(to_jsonb(NEW),'insert');
 ELSIF ROW(OLD.uuid,OLD.raw_content,OLD.title,OLD.source_site,OLD.source_url,OLD.publish_date,OLD.is_deleted)
       IS DISTINCT FROM ROW(NEW.uuid,NEW.raw_content,NEW.title,NEW.source_site,NEW.source_url,NEW.publish_date,NEW.is_deleted) THEN
  PERFORM publication_record_document(to_jsonb(NEW),'update');
 END IF;
 IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND OLD.is_deleted IS DISTINCT FROM NEW.is_deleted) THEN
  value := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) || '{"is_deleted":true}'::jsonb ELSE to_jsonb(NEW) END;
  INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
  VALUES ('graph:'||(value->>'uuid')||':'||txid_current()::text,'graph',value->>'uuid',
          jsonb_build_object('document',publication_document_snapshot(value),'capture_reason','source_state_changed'))
  ON CONFLICT(event_key) DO NOTHING;
 END IF;
 RETURN NULL;
END
$$;

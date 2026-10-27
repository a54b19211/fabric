-- 原采集数据库中的持久发表待办；不网络、不调用图谱或算法。
CREATE TABLE IF NOT EXISTS publication_outbox (
 id bigserial PRIMARY KEY,
 event_key text NOT NULL UNIQUE,
 kind text NOT NULL CHECK (kind IN ('document','graph','backfill','control')),
 source_doc_uuid text NOT NULL,
 snapshot jsonb NOT NULL,
 prepared jsonb,
 state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','processing','published','blocked','failed')),
 attempts integer NOT NULL DEFAULT 0,
 lease_token text,
 lease_until timestamptz,
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 completed_at timestamptz,
 receipt jsonb,
 error jsonb
);
CREATE INDEX IF NOT EXISTS publication_outbox_pending ON publication_outbox(state,next_attempt_at,id);
CREATE INDEX IF NOT EXISTS publication_outbox_document_order ON publication_outbox(source_doc_uuid,id);

CREATE OR REPLACE FUNCTION publication_document_snapshot(value jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
 SELECT jsonb_build_object(
 'uuid',value->'uuid','title',value->'title','raw_content',value->'raw_content',
 'source_site',value->'source_site','source_url',value->'source_url',
 'publish_date',value->'publish_date','created_at',value->'created_at',
 'updated_at',value->'updated_at','is_deleted',COALESCE(value->'is_deleted','false'::jsonb))
$$;

CREATE OR REPLACE FUNCTION publication_record_document(value jsonb, reason text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE frozen jsonb; digest text; previous text; event_id bigint;
BEGIN
 -- 按文档串行比较最近成功清洗的候选，不按正文hash永久去重；合法A→B→A须保留。
 PERFORM pg_advisory_xact_lock(hashtextextended(value->>'uuid',0));
 frozen := publication_document_snapshot(value);
 digest := encode(sha256(convert_to((frozen - ARRAY['updated_at','created_at'])::text,'UTF8')),'hex');
 SELECT snapshot->>'content_fingerprint' INTO previous FROM publication_outbox
  WHERE kind='document' AND source_doc_uuid=value->>'uuid' ORDER BY id DESC LIMIT 1;
 IF previous=digest THEN RETURN; END IF;
 event_id := nextval(pg_get_serial_sequence('publication_outbox','id'));
 INSERT INTO publication_outbox(id,event_key,kind,source_doc_uuid,snapshot)
 VALUES (event_id,'document:'||(value->>'uuid')||':'||event_id::text,'document',value->>'uuid',
         jsonb_build_object('document',frozen,'capture_reason',reason,'content_fingerprint',digest));
END
$$;

CREATE OR REPLACE FUNCTION publication_document_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE value jsonb;
BEGIN
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
DROP TRIGGER IF EXISTS publication_document_write ON documents;
CREATE TRIGGER publication_document_write AFTER INSERT OR UPDATE OR DELETE ON documents
 FOR EACH ROW EXECUTE FUNCTION publication_document_changed();

CREATE OR REPLACE FUNCTION publication_graph_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE value jsonb; previous jsonb; doc text; relevant boolean;
BEGIN
 value := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 previous := CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
 -- 只关注曾经或现在获审核/自动审核的行；pending队列/推送状态不产生图发布。
 relevant := COALESCE(value->>'review_status','') IN ('approved','auto_approved')
          OR COALESCE(previous->>'review_status','') IN ('approved','auto_approved');
 IF NOT relevant THEN RETURN NULL; END IF;
 IF TG_OP='UPDATE' AND (value - ARRAY['updated_at','weknora_graph_status','weknora_sync_status'])
      = (previous - ARRAY['updated_at','weknora_graph_status','weknora_sync_status']) THEN RETURN NULL; END IF;
 doc := value->>'source_doc_uuid';
 IF doc IS NULL OR doc='' THEN RETURN NULL; END IF;
 INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
 VALUES ('graph:'||doc||':'||txid_current()::text,'graph',doc,
         jsonb_build_object('source_doc_uuid',doc,'capture_reason','reviewed_graph_dirty'))
 ON CONFLICT(event_key) DO NOTHING;
 RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS publication_entity_write ON extracted_entities;
CREATE TRIGGER publication_entity_write AFTER INSERT OR UPDATE OR DELETE ON extracted_entities
 FOR EACH ROW EXECUTE FUNCTION publication_graph_changed();
DROP TRIGGER IF EXISTS publication_relation_write ON entity_relations;
CREATE TRIGGER publication_relation_write AFTER INSERT OR UPDATE OR DELETE ON entity_relations
 FOR EACH ROW EXECUTE FUNCTION publication_graph_changed();

-- 字典资格撤销并不必然改mention；只传播已被引用的资格变化，不发布全字典别名。
CREATE OR REPLACE FUNCTION publication_dict_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE identity_id bigint;
BEGIN
 IF TG_OP='UPDATE' AND OLD.is_deleted IS NOT DISTINCT FROM NEW.is_deleted
    AND OLD.is_verified IS NOT DISTINCT FROM NEW.is_verified THEN RETURN NULL; END IF;
 identity_id := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
 INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
 SELECT DISTINCT 'graph:'||e.source_doc_uuid||':'||txid_current()::text,'graph',e.source_doc_uuid,
  jsonb_build_object('source_doc_uuid',e.source_doc_uuid,'capture_reason','dictionary_qualification_changed')
 FROM extracted_entities e WHERE e.dict_id=identity_id AND NOT e.is_deleted
  AND e.review_status IN ('approved','auto_approved') AND e.source_doc_uuid<>''
 ON CONFLICT(event_key) DO NOTHING;
 RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS publication_dictionary_write ON entity_dict;
CREATE TRIGGER publication_dictionary_write AFTER UPDATE OR DELETE ON entity_dict
 FOR EACH ROW EXECUTE FUNCTION publication_dict_changed();

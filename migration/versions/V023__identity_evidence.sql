-- Additive identity evidence and review metadata. Existing review flags are not rewritten.
ALTER TABLE entity_dict ADD COLUMN IF NOT EXISTS verified_by TEXT;
ALTER TABLE entity_dict ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE entity_dict ADD COLUMN IF NOT EXISTS verification_basis TEXT;

ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS source_version_id TEXT;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS source_quote TEXT;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS source_char_start INTEGER;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS source_char_end INTEGER;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS source_version_id TEXT;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS source_quote TEXT;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS source_char_start INTEGER;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS source_char_end INTEGER;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS entity_source_evidence (
    id BIGSERIAL PRIMARY KEY,
    object_type TEXT NOT NULL CHECK (object_type IN ('entity', 'relation')),
    object_id BIGINT NOT NULL,
    source_doc_uuid TEXT NOT NULL,
    source_site TEXT NOT NULL DEFAULT '',
    version_id TEXT,
    quote TEXT NOT NULL DEFAULT '',
    char_start INTEGER,
    char_end INTEGER,
    match_step TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    evidence_key TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_entity_source_evidence UNIQUE (object_type, object_id, evidence_key),
    CHECK (char_start IS NULL OR char_start >= 0),
    CHECK (char_end IS NULL OR char_end >= 0),
    CHECK (char_start IS NULL OR char_end IS NULL OR char_end >= char_start),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS idx_entity_evidence_document ON entity_source_evidence(source_doc_uuid, object_type);
CREATE INDEX IF NOT EXISTS idx_entity_evidence_key ON entity_source_evidence(evidence_key);

ALTER TABLE entity_identity_merge_proposal ADD COLUMN IF NOT EXISTS match_step TEXT DEFAULT '';
ALTER TABLE entity_identity_merge_proposal ADD COLUMN IF NOT EXISTS rule_version TEXT;
ALTER TABLE entity_identity_merge_proposal ADD COLUMN IF NOT EXISTS model_version TEXT;
ALTER TABLE entity_identity_merge_proposal ADD COLUMN IF NOT EXISTS candidate_payload JSONB DEFAULT '{}';
ALTER TABLE entity_identity_merge_proposal ADD COLUMN IF NOT EXISTS evidence_key TEXT;
ALTER TABLE entity_identity_merge_proposal ADD COLUMN IF NOT EXISTS merge_log_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_proposal_evidence ON entity_identity_merge_proposal(evidence_key);

CREATE TABLE IF NOT EXISTS entity_primekg_mapping_proposal (
    id BIGSERIAL PRIMARY KEY,
    dict_id BIGINT NOT NULL,
    namespace TEXT NOT NULL,
    native_id TEXT NOT NULL,
    method TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (score >= 0 AND score <= 1),
    evidence JSONB NOT NULL DEFAULT '{}',
    proposal_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
    reviewer TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_primekg_mapping_dict_status ON entity_primekg_mapping_proposal(dict_id, status);

ALTER TABLE entity_merge_log ADD COLUMN IF NOT EXISTS rolled_back_by TEXT;

ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS identity_confirmed_by TEXT;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS identity_confirmed_at TIMESTAMPTZ;
ALTER TABLE extracted_entities ADD COLUMN IF NOT EXISTS identity_confirmation_basis TEXT;

CREATE INDEX IF NOT EXISTS idx_eml_absorbed_dict_ids ON entity_merge_log USING GIN (absorbed_dict_ids);

-- Additional recorded origins share the existing graph outbox and transaction event key.
-- The guard preserves compatibility when V021 is applied before optional publication V019.
CREATE OR REPLACE FUNCTION publication_record_graph(doc text, reason text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 IF doc IS NULL OR doc='' OR to_regclass('publication_outbox') IS NULL THEN RETURN; END IF;
 INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
 VALUES ('graph:'||doc||':'||txid_current()::text,'graph',doc,
         jsonb_build_object('source_doc_uuid',doc,'capture_reason',reason))
 ON CONFLICT(event_key) DO NOTHING;
END
$$;

CREATE OR REPLACE FUNCTION publication_source_evidence_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE value jsonb; status text;
BEGIN
 value := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 IF value->>'object_type'='entity' THEN
  SELECT review_status INTO status FROM extracted_entities WHERE id=(value->>'object_id')::bigint AND NOT is_deleted;
 ELSIF value->>'object_type'='relation' THEN
  SELECT review_status INTO status FROM entity_relations WHERE id=(value->>'object_id')::bigint AND NOT is_deleted;
 END IF;
 IF status IN ('approved','auto_approved') THEN
  PERFORM publication_record_graph(value->>'source_doc_uuid','recorded_source_evidence_changed');
 END IF;
 RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS publication_source_evidence_write ON entity_source_evidence;
CREATE TRIGGER publication_source_evidence_write AFTER INSERT OR UPDATE OR DELETE ON entity_source_evidence
 FOR EACH ROW EXECUTE FUNCTION publication_source_evidence_changed();

CREATE OR REPLACE FUNCTION publication_recorded_origins_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE value jsonb; previous jsonb; kind text; doc text;
BEGIN
 value := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 previous := CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
 IF COALESCE(value->>'review_status','') NOT IN ('approved','auto_approved')
    AND COALESCE(previous->>'review_status','') NOT IN ('approved','auto_approved') THEN RETURN NULL; END IF;
 IF TG_OP='UPDATE' AND (value - ARRAY['updated_at','weknora_graph_status','weknora_sync_status'])
    = (previous - ARRAY['updated_at','weknora_graph_status','weknora_sync_status']) THEN RETURN NULL; END IF;
 kind := CASE WHEN TG_TABLE_NAME='extracted_entities' THEN 'entity' ELSE 'relation' END;
 FOR doc IN SELECT DISTINCT source_doc_uuid FROM entity_source_evidence
            WHERE object_type=kind AND object_id=(value->>'id')::bigint LOOP
  PERFORM publication_record_graph(doc,'recorded_object_review_changed');
 END LOOP;
 RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS publication_entity_recorded_origins ON extracted_entities;
CREATE TRIGGER publication_entity_recorded_origins AFTER UPDATE OR DELETE ON extracted_entities
 FOR EACH ROW EXECUTE FUNCTION publication_recorded_origins_changed();
DROP TRIGGER IF EXISTS publication_relation_recorded_origins ON entity_relations;
CREATE TRIGGER publication_relation_recorded_origins AFTER UPDATE OR DELETE ON entity_relations
 FOR EACH ROW EXECUTE FUNCTION publication_recorded_origins_changed();

CREATE OR REPLACE FUNCTION publication_dictionary_recorded_origins_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE identity_id bigint; doc text;
BEGIN
 IF TG_OP='UPDATE' AND ROW(OLD.is_deleted,OLD.is_verified,OLD.verified_by,OLD.verified_at,OLD.verification_basis)
     IS NOT DISTINCT FROM ROW(NEW.is_deleted,NEW.is_verified,NEW.verified_by,NEW.verified_at,NEW.verification_basis) THEN RETURN NULL; END IF;
 identity_id := CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
 FOR doc IN SELECT DISTINCT proof.source_doc_uuid FROM entity_source_evidence proof
            JOIN extracted_entities e ON proof.object_type='entity' AND proof.object_id=e.id
            WHERE e.dict_id=identity_id AND NOT e.is_deleted AND e.review_status IN ('approved','auto_approved') LOOP
  PERFORM publication_record_graph(doc,'recorded_dictionary_qualification_changed');
 END LOOP;
 RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS publication_dictionary_recorded_origins ON entity_dict;
CREATE TRIGGER publication_dictionary_recorded_origins AFTER UPDATE OR DELETE ON entity_dict
 FOR EACH ROW EXECUTE FUNCTION publication_dictionary_recorded_origins_changed();

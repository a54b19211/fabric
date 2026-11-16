-- Attachment versions are picked by clean/derived_text.py (role = attachment, in published order). Without this
-- partial index the pick reads the payload of every version of the attachment sources and hits the 30 s timeout.
CREATE INDEX IF NOT EXISTS fabric_versions_attachment_pick ON fabric_versions (source_id, published_seq)
  WHERE (payload->'facts'->>'role') = 'attachment';

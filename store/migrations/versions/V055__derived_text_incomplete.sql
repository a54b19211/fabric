-- 2026-11-21：附件解析多一个终态 incomplete（NMPA 审计 A6）：解析出来的字数明显少于独立对照（docx 的 XML 全文、
-- .doc 的 LibreOffice 纯文本），或 MinerU 读 PDF 有内容的页明显少于总页数，不当成 parsed。正文照存，便于复核。
ALTER TABLE derived_clean_text DROP CONSTRAINT IF EXISTS derived_clean_text_status_check;
ALTER TABLE derived_clean_text ADD CONSTRAINT derived_clean_text_status_check
  CHECK (status IN ('parsed','failed','unsupported','incomplete'));

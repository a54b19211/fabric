from db.repository import upsert_doc_extra


def db_writer(doc_uuid: str, parsed_result: dict) -> None:
    """将 PubMed 专属字段写入 doc_extra 表。"""
    # 从 parsed_result 中提取 PubMed 特有字段
    extra = {
        "pmid": parsed_result.get("pmid", ""),
        "pmcid": parsed_result.get("pmcid", ""),
        "doi": parsed_result.get("doi", ""),
        "journal_name": parsed_result.get("journal_name", ""),
        "authors": parsed_result.get("authors", []),
        "fulltext_status": parsed_result.get("fulltext_status", "abstract_only"),
        "oa_pdf_url": parsed_result.get("oa_pdf_url", ""),
    }
    
    # 过滤空值
    extra = {k: v for k, v in extra.items() if v not in [None, "", []]}
    
    if extra:
        upsert_doc_extra(doc_uuid, "pubmed", extra)
        print(f"[PubMed] 写入 doc_extra: {doc_uuid} | 字段: {list(extra.keys())}")
    else:
        print(f"[PubMed] 无特有字段可写入 doc_extra: {doc_uuid}")
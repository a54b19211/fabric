from db.repository import upsert_doc_extra


def db_writer(doc_uuid: str, parsed_result: dict) -> None:
    """将 chictr 专属字段写入 doc_extra 表。"""
    extra = parsed_result.get("extra", {})
    if not extra:
        return
    upsert_doc_extra(doc_uuid, "chictr", extra)

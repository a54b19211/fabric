from db.repository import upsert_doc_extra


def db_writer(doc_uuid: str, parsed_result: dict) -> None:
    """将 hkex 专属字段写入 doc_extra 表。"""
    extra = {
        "stock_code": parsed_result.get("stock_code", ""),
        "stock_name": parsed_result.get("stock_name", ""),
        "stock_id":   parsed_result.get("stock_id", 0),
    }
    upsert_doc_extra(doc_uuid, "hkex", extra)

from db.models import HkexMeta
from db.session import get_session


def db_writer(doc_uuid: str, parsed_result: dict) -> None:
    """将 hkex 专属字段写入 hkex_meta 表。"""
    with get_session() as session:
        if session is None:
            return
        existing = session.get(HkexMeta, doc_uuid)
        if existing:
            existing.stock_code = parsed_result.get("stock_code", "")
            existing.stock_name = parsed_result.get("stock_name", "")
            existing.stock_id = parsed_result.get("stock_id", 0)
        else:
            session.add(HkexMeta(
                uuid=doc_uuid,
                stock_code=parsed_result.get("stock_code", ""),
                stock_name=parsed_result.get("stock_name", ""),
                stock_id=parsed_result.get("stock_id", 0),
            ))

"""
修复脚本：重爬所有 extraction_status=done 且 raw_content 为空的文档。

用法：
    cd backend
    python -m api.scripts.repair_empty_raw_content
"""
import time
import threading

from loguru import logger
from db.session import init_db, get_session
from db.models import Document


def main():
    init_db()

    with get_session() as session:
        if session is None:
            logger.error("数据库未连接")
            return

        docs = session.query(Document).filter(
            Document.extraction_status == "done",
            Document.is_deleted == False,
            Document.raw_content == "",
        ).all()

    if not docs:
        logger.info("没有需要修复的文档")
        return

    uuids = [d.uuid for d in docs]
    logger.info(f"共 {len(uuids)} 篇 done 文档 raw_content 为空，开始重爬...")

    from flows.crawl_flow import recrawl_document_fn

    ok, fail = 0, 0
    for uuid in uuids:
        try:
            t = threading.Thread(target=recrawl_document_fn, args=(uuid,), daemon=True)
            t.start()
            ok += 1
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"[Repair] 触发重爬失败: {uuid} | {e}")
            fail += 1

    logger.info(f"[Repair] 完成: 已触发 {ok} 篇, 失败 {fail} 篇")


if __name__ == "__main__":
    main()

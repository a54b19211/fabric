# -*- coding: utf-8 -*-
"""文档查询接口"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import RecrawlRequest, BatchRecrawlRequest
from api.schemas.response import (
    DocumentListResponse, DocumentDetailResponse,
    DocumentItem, AttachmentItem, RecrawlResponse, BatchRecrawlResponse,
)
from db.repository import (
    is_db_connected, list_documents, get_document_detail, get_document_exists, get_documents_by_uuids,
)

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=DocumentListResponse)
def list_documents_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source_site: str = Query(""),
    doc_status: str = Query(""),
    extraction_status: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    """文档列表查询（分页 + 多条件过滤）"""
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    try:
        result = list_documents(
            page=page, page_size=page_size, source_site=source_site,
            doc_status=doc_status, extraction_status=extraction_status,
            date_from=date_from, date_to=date_to,
        )
        items = [
            DocumentItem(
                id=it["id"], uuid=it["uuid"], title=it["title"],
                source_site=it["source_site"], publish_date=it["publish_date"],
                doc_status=it["doc_status"], confidence_score=it["confidence_score"],
                extraction_status=it["extraction_status"], weknora_sync_status=it["weknora_sync_status"],
            )
            for it in result["items"]
        ]
        return DocumentListResponse(total=result["total"], page=page, page_size=page_size, items=items)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 文档列表查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get("/{uuid}", response_model=DocumentDetailResponse)
def get_document(uuid: str):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    try:
        doc = get_document_detail(uuid)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        return DocumentDetailResponse(
            id=doc["id"], uuid=doc["uuid"], title=doc["title"],
            raw_content=doc["raw_content"], source_site=doc["source_site"],
            publish_date=doc["publish_date"], doc_status=doc["doc_status"],
            confidence_score=doc["confidence_score"], parse_method=doc["parse_method"],
            doc_type=doc["doc_type"], language=doc["language"],
            extra=doc["extra"],
            attachments=[AttachmentItem(**a) for a in doc["attachments"]],
            extraction_status=doc["extraction_status"], extraction_model=doc["extraction_model"],
            extraction_error=doc["extraction_error"], extracted_at=doc["extracted_at"],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 文档详情查询失败: {uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/{uuid}/recrawl", response_model=RecrawlResponse)
def recrawl_document(uuid: str, body: RecrawlRequest = RecrawlRequest()):
    if not uuid or not uuid.strip():
        raise HTTPException(status_code=400, detail="uuid 不能为空")

    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    try:
        doc = get_document_exists(uuid)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        if not doc["source_url"]:
            raise HTTPException(status_code=422, detail="文档无 source_url，无法重爬")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 重爬文档校验失败: {uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"校验失败: {str(e)}")

    try:
        import threading
        from flows.crawl_flow import recrawl_document_fn
        t = threading.Thread(target=recrawl_document_fn, args=(uuid,), daemon=True)
        t.start()
        return RecrawlResponse(success=True, uuid=uuid, message="重爬任务已提交，请稍后刷新查看状态")
    except Exception as e:
        logger.error(f"[API] 重爬文档失败: {uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"重爬失败: {str(e)}")


@router.post("/batch-recrawl", response_model=BatchRecrawlResponse)
def batch_recrawl_documents(body: BatchRecrawlRequest):
    if not body.uuids:
        raise HTTPException(status_code=400, detail="uuids 列表不能为空")

    import threading
    from flows.crawl_flow import recrawl_document_fn

    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    try:
        docs = get_documents_by_uuids(body.uuids)
        found_uuids = {d["uuid"] for d in docs}
        missing = [u for u in body.uuids if u not in found_uuids]
        no_url = [d["uuid"] for d in docs if not d["source_url"]]
        valid = [d["uuid"] for d in docs if d["source_url"]]
        skipped = missing + no_url
    except Exception as e:
        logger.error(f"[API] 批量重爬校验失败: {e}")
        raise HTTPException(status_code=500, detail=f"校验失败: {str(e)}")

    for uid in valid:
        try:
            t = threading.Thread(target=recrawl_document_fn, args=(uid,), daemon=True)
            t.start()
        except Exception as e:
            logger.error(f"[API] 重爬触发失败: {uid} | {e}")

    msg = f"已触发 {len(valid)} 个文档的重爬任务"
    if missing:
        msg += f"，{len(missing)} 个文档不存在"
    if no_url:
        msg += f"，{len(no_url)} 个文档无 source_url 跳过"
    return BatchRecrawlResponse(success=True, triggered=len(valid), skipped=len(skipped), message=msg)

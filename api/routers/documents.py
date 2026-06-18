# -*- coding: utf-8 -*-
"""文档查询接口"""
import hashlib
import uuid as _uuid
from typing import List

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from loguru import logger

from api.schemas.request import RecrawlRequest, BatchRecrawlRequest
from api.schemas.response import (
    DocumentListResponse, DocumentDetailResponse,
    DocumentItem, AttachmentItem, RecrawlResponse, BatchRecrawlResponse,
    UploadResponse, UploadItem, BatchUploadResponse,
)
from db.session import get_session
from db.models import Document, DocExtra, Attachment
from db.repository import upsert_document
from biz_rules.schemas import MetadataEnvelope, ProvenanceInfo, ConfidenceInfo
from sqlalchemy import func as sqlfunc

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("/sites")
def get_source_sites():
    """返回 documents 表中所有去重后的 source_site，前端筛选下拉用。"""
    with get_session() as session:
        if session is None:
            return {"sites": []}
        rows = (
            session.query(Document.source_site)
            .filter(Document.is_deleted == False, Document.source_site != "")
            .distinct()
            .order_by(Document.source_site)
            .all()
        )
        sites = [r[0] for r in rows if r[0]]
        return {"sites": sites}


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source_site: str = Query(""),
    doc_status: str = Query(""),
    extraction_status: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    """文档列表查询（分页 + 多条件过滤）"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            # 只查列表需要的列，避免拉 raw_content 等大字段
            cols = (
                Document.id, Document.uuid, Document.title,
                Document.source_site, Document.publish_date, Document.doc_status,
                Document.confidence_score, Document.extraction_status,
                Document.weknora_sync_status,
                Document.updated_at,
            )
            q = (
                session.query(*cols)
                .filter(Document.is_deleted == False)
            )

            # 动态筛选条件
            if source_site:
                q = q.filter(Document.source_site == source_site)
            if doc_status:
                q = q.filter(Document.doc_status == doc_status)
            if extraction_status:
                q = q.filter(Document.extraction_status == extraction_status)
            if date_from:
                q = q.filter(Document.publish_date >= date_from)
            if date_to:
                q = q.filter(Document.publish_date <= date_to)

            # 获取总数（用轻量 count，不走子查询）
            total = session.query(sqlfunc.count(Document.id)).filter(
                Document.is_deleted == False,
                *([Document.source_site == source_site] if source_site else []),
                *([Document.doc_status == doc_status] if doc_status else []),
                *([Document.extraction_status == extraction_status] if extraction_status else []),
                *([Document.publish_date >= date_from] if date_from else []),
                *([Document.publish_date <= date_to] if date_to else []),
            ).scalar()

            # 分页查询
            rows = (
                q.order_by(Document.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )

            # 转换为响应模型
            items = [
                DocumentItem(
                    id=r.id,
                    uuid=r.uuid,
                    title=r.title or "",
                    source_site=r.source_site or "",
                    publish_date=r.publish_date or "",
                    doc_status=r.doc_status or "",
                    confidence_score=r.confidence_score or 0.0,
                    extraction_status=r.extraction_status or "",
                    weknora_sync_status=r.weknora_sync_status or "",
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                )
                for r in rows
            ]
            
            logger.info(f"[API] 文档列表查询: page={page}, page_size={page_size}, total={total}")
            return DocumentListResponse(total=total, page=page, page_size=page_size, items=items)
            
        except Exception as e:
            logger.error(f"[API] 文档列表查询失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get("/{uuid}", response_model=DocumentDetailResponse)
def get_document(uuid: str):
    """文档详情查询（含全文、附件、扩展字段）"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            # 查询主文档
            doc = session.query(Document).filter(
                Document.uuid == uuid, 
                Document.is_deleted == False
            ).first()
            
            if doc is None:
                logger.warning(f"[API] 文档不存在: {uuid}")
                raise HTTPException(status_code=404, detail="文档不存在")

            # 查询扩展字段
            extra_row = session.query(DocExtra).filter(DocExtra.uuid == uuid).first()
            extra = extra_row.extra if extra_row else {}

            # 查询附件
            att_rows = session.query(Attachment).filter(
                Attachment.parent_uuid == uuid, 
                Attachment.is_deleted == False
            ).all()
            attachments = [
                AttachmentItem(
                    file_name=a.file_name or "",
                    file_url=a.file_url or "",
                    local_path=a.local_path or "",
                )
                for a in att_rows
            ]

            # 构建响应
            response = DocumentDetailResponse(
                id=doc.id,
                uuid=doc.uuid,
                title=doc.title or "",
                raw_content=doc.raw_content or "",
                source_site=doc.source_site or "",
                publish_date=doc.publish_date or "",
                doc_status=doc.doc_status or "",
                confidence_score=doc.confidence_score or 0.0,
                parse_method=doc.parse_method or "",
                doc_type=doc.doc_type or "",
                language=doc.language or "",
                extra=extra,
                attachments=attachments,
                extraction_status=doc.extraction_status or "",
                extraction_model=doc.extraction_model or "",
                extraction_error=doc.extraction_error or "",
                extracted_at=doc.extracted_at.isoformat() if doc.extracted_at else None,
            )
            
            logger.info(f"[API] 文档详情查询: {uuid}")
            return response
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] 文档详情查询失败: {uuid} | {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/{uuid}/recrawl", response_model=RecrawlResponse)
def recrawl_document(uuid: str, body: RecrawlRequest = RecrawlRequest()):
    """
    重爬指定文档。

    流程：
      1. 校验文档存在且有 source_url
      2. 在后台线程提交 recrawl_document_flow（Prefect flow run，UI 可见）
      3. 立即返回，不等待完成

    重爬会：
      - 软删除旧的 extracted_entities / entity_relations
      - 重置 extraction_status / weknora_sync_status 为 pending
      - 重新抓取页面并 upsert 写库
    """
    # UUID 格式校验
    if not uuid or not uuid.strip():
        raise HTTPException(status_code=400, detail="uuid 不能为空")
    
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")
        
        try:
            doc = (
                session.query(Document.uuid, Document.source_url)
                .filter(
                    Document.uuid == uuid, Document.is_deleted == False
                ).first()
            )
            if doc is None:
                logger.warning(f"[API] 文档不存在: {uuid}")
                raise HTTPException(status_code=404, detail="文档不存在")
            if not doc.source_url:
                logger.warning(f"[API] 文档无 source_url: {uuid}")
                raise HTTPException(status_code=422, detail="文档无 source_url，无法重爬")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] 重爬文档校验失败: {uuid} | {e}")
            raise HTTPException(status_code=500, detail=f"校验失败: {str(e)}")

    try:
        import threading
        from flows.crawl_flow import recrawl_document_fn

        t = threading.Thread(
            target=recrawl_document_fn,
            args=(uuid,),
            daemon=True,
        )
        t.start()

        logger.info(f"[API] 重爬已触发: {uuid}")
        return RecrawlResponse(
            success=True,
            uuid=uuid,
            message="重爬任务已提交，请稍后刷新查看状态",
        )
    except Exception as e:
        logger.error(f"[API] 重爬文档失败: {uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"重爬失败: {str(e)}")


@router.post("/batch-recrawl", response_model=BatchRecrawlResponse)
def batch_recrawl_documents(body: BatchRecrawlRequest):
    """
    批量重爬指定文档列表。

    流程：
      1. 校验每个 uuid 存在且有 source_url
      2. 在后台线程逐个提交 recrawl_document_fn
      3. 立即返回，不等待完成
    """
    if not body.uuids:
        raise HTTPException(status_code=400, detail="uuids 列表不能为空")

    import threading
    from flows.crawl_flow import recrawl_document_fn

    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            docs = (
                session.query(Document.uuid, Document.source_url)
                .filter(
                    Document.uuid.in_(body.uuids),
                    Document.is_deleted == False,
                )
                .all()
            )

            found_uuids = {d.uuid for d in docs}
            missing = [u for u in body.uuids if u not in found_uuids]
            no_url = [d.uuid for d in docs if not d.source_url]
            valid = [d.uuid for d in docs if d.source_url]

            skipped = missing + no_url
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] 批量重爬校验失败: {e}")
            raise HTTPException(status_code=500, detail=f"校验失败: {str(e)}")

    # 在后台线程中逐个触发重爬
    for uid in valid:
        try:
            t = threading.Thread(
                target=recrawl_document_fn,
                args=(uid,),
                daemon=True,
            )
            t.start()
        except Exception as e:
            logger.error(f"[API] 重爬触发失败: {uid} | {e}")

    msg = f"已触发 {len(valid)} 个文档的重爬任务"
    if missing:
        msg += f"，{len(missing)} 个文档不存在"
    if no_url:
        msg += f"，{len(no_url)} 个文档无 source_url 跳过"

    logger.info(f"[API] 批量重爬: triggered={len(valid)}, skipped={len(skipped)}")
    return BatchRecrawlResponse(
        success=True,
        triggered=len(valid),
        skipped=len(skipped),
        message=msg,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    source_site: str = Form(default=""),
):
    """上传本地 Markdown 文件到文档库。

    流程：
      1. 校验文件类型为 .md
      2. 读取文件内容，用 SHA256 生成 UUID5（幂等）
      3. 从第一个 # 标题提取 title，fallback 用文件名
      4. 构建 MetadataEnvelope 调用 upsert_document
    """
    if not file.filename or not file.filename.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="仅支持 .md 文件")

    try:
        content = await file.read()
    except Exception as e:
        logger.error(f"[API] 读取上传文件失败: {e}")
        raise HTTPException(status_code=400, detail=f"读取文件失败: {str(e)}")

    raw_content = content.decode("utf-8", errors="replace")
    if not raw_content.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")

    filename = file.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    uuid_str, title = _ingest_md_content(raw_content, filename, source_site)

    logger.info(f"[API] 文档上传成功: uuid={uuid_str}, title={title}")
    return UploadResponse(
        success=True,
        uuid=uuid_str,
        title=title,
        message="上传成功",
    )


@router.post("/upload/batch", response_model=BatchUploadResponse)
async def batch_upload_documents(
    files: List[UploadFile] = File(...),
    source_site: str = Form(default=""),
):
    """批量上传本地 Markdown 文件到文档库。"""
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一个文件")

    results: list[UploadItem] = []
    for file in files:
        item = UploadItem(filename=file.filename or "")
        try:
            if not file.filename or not file.filename.lower().endswith(".md"):
                item.error = "仅支持 .md 文件"
                results.append(item)
                continue

            content = await file.read()
            raw_content = content.decode("utf-8", errors="replace")
            if not raw_content.strip():
                item.error = "文件内容为空"
                results.append(item)
                continue

            filename = file.filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            uuid_str, title = _ingest_md_content(raw_content, filename, source_site)

            item.uuid = uuid_str
            item.title = title
            item.success = True
        except Exception as e:
            logger.error(f"[API] 批量上传失败: {file.filename} | {e}")
            item.error = str(e)
        results.append(item)

    succeeded = sum(1 for r in results if r.success)
    failed = len(results) - succeeded
    logger.info(f"[API] 批量上传完成: total={len(results)}, succeeded={succeeded}, failed={failed}")
    return BatchUploadResponse(
        total=len(results),
        succeeded=succeeded,
        failed=failed,
        items=results,
    )


def _ingest_md_content(raw_content: str, filename: str, source_site: str) -> tuple[str, str]:
    """处理单篇 md 内容：生成 UUID、写入数据库。返回 (uuid, title)。"""
    title = filename
    for line in raw_content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip()
            break

    content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    doc_uuid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"local_md:{content_hash}"))

    envelope = MetadataEnvelope(
        uuid=doc_uuid,
        title=title,
        raw_content=raw_content,
        provenance=ProvenanceInfo(
            source_url="",
            source_site=source_site,
            local_file_path=filename,
        ),
        confidence=ConfidenceInfo(score=1.0, reason="本地手动上传"),
        parse_method="local_upload",
        status="AUTO_PROCESSED",
        doc_type="",
        language="",
    )

    try:
        upsert_document(envelope)
    except Exception as e:
        logger.error(f"[API] 上传文档写入失败: {doc_uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"写入失败: {str(e)}")

    return doc_uuid, title

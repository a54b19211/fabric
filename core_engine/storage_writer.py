"""
数据落盘引擎 —— 数据必须落库。
本地文件存储仅作为调试/开发辅助（ENABLE_LOCAL_STORAGE=True 时启用）。
"""

import json
import types
from pathlib import Path
from datetime import datetime, timezone
from typing import Union, get_args, get_origin

from loguru import logger
from prefect import task

from config.settings import APP
from config.crawler_settings import SITE_PROFILES
from biz_rules.schemas import (
    EXTRA_MODEL_MAP,
    MetadataEnvelope,
    ProvenanceInfo,
    ConfidenceInfo,
    AttachmentInfo,
    validate_extra,
)
from biz_rules.document_validator import validate_document
from biz_rules.source_mapper import calculate_confidence
from db.repository import upsert_document, upsert_authority_resolutions
from core_engine.lineage_store import save_lineage
from core_engine.authority_store import save_authority_resolutions, summarize


# 站点字段白名单（兜底用）：站点未在 EXTRA_MODEL_MAP 声明时才启用
_SITE_FIELD_FALLBACK = ("drug_name", "approval_number", "manufacturer")


def _has_value(val) -> bool:
    """判断字段是否「有实质内容」：空串/空白/空列表/0 都算空缺。"""
    if isinstance(val, list):
        return len(val) > 0
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    return bool(str(val or "").strip())


def _unwrap_optional(ann):
    """把 Optional[X] / X | None 解包成 X，其余原样返回。

    必要性：站点 Extra 模型里不少字段是 Optional 语义（如
    `enrollment: Optional[int] = None` 表示「未知不补零」），
    而早期实现用 `ann is int` 做精确比对，遇到 Optional[int] 会
    一路落到「只接受 list/tuple」的分支，把合法的 int 值判为类型不符。
    """
    origin = get_origin(ann)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ann


def _accepts_value(model_cls, key: str, val) -> bool:
    """按模型字段注解判断该值能否补进 extra（防类型错配）。

    无对应模型时保持保守：只接受字符串（与旧白名单行为一致）。
    """
    if model_cls is None or key not in model_cls.model_fields:
        return isinstance(val, str)
    ann = _unwrap_optional(model_cls.model_fields[key].annotation)
    if ann is str:
        return isinstance(val, str)
    if ann is bool:
        return isinstance(val, bool)
    if ann is int:
        return isinstance(val, int) and not isinstance(val, bool)
    if ann is float:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    return isinstance(val, (list, tuple))


def _merge_site_fields(raw_extra: dict, structured: dict, site: str = "") -> dict:
    """把 structured_data 中的站点字段补进 extra（只补空缺，不覆盖已有值）。

    补齐键集按站点 Extra 模型声明取（EXTRA_MODEL_MAP），而不是硬编码白名单。
    原因：各站点 fetcher 落字段的容器不一致 —— CSS 抽取器写 extra，
    API / fetcher 写 structured_data。此前白名单只列了 3 个字段，导致
    pubmed 的 pmid / doi / pmcid / journal_name / authors 等标识符整批
    停在 structured_data，extra 只剩 Pydantic 默认值，T3 标识符对齐
    因此拿不到任何本地可校验的标识符。
    """
    merged = dict(raw_extra or {})
    structured = structured or {}

    model_cls = EXTRA_MODEL_MAP.get(site)
    keys = tuple(model_cls.model_fields) if model_cls else _SITE_FIELD_FALLBACK

    for key in keys:
        if _has_value(merged.get(key)):
            continue
        val = structured.get(key)
        if _has_value(val) and _accepts_value(model_cls, key, val):
            merged[key] = val
    return merged


@task(name="write_to_database")
def write_to_database(parsed_result: dict) -> dict:
    """
    数据落库：将解析结果写入数据库。

    参数:
        parsed_result: 经 quality_checker 检验通过的解析结果

    返回:
        dict: {"uuid": ..., "status": ..., "confidence_score": ..., "success": bool}
    """
    doc_uuid = parsed_result["uuid"]
    site = parsed_result.get("source_site", "unknown") or "unknown"

    # 组装元数据信封
    structured = parsed_result.get("structured_data", {}) or {}
    sniffed = parsed_result.get("sniffed_meta", {}) or {}

    # 计算置信度
    confidence_result = calculate_confidence(
        source_url=parsed_result.get("source_url", ""),
        doi=sniffed.get("doi", ""),
        journal_name=sniffed.get("journal_name", ""),
    )

    # 判定审核状态
    status = "AUTO_PROCESSED" if confidence_result["score"] >= 0.8 else "PENDING_REVIEW"

    # 组装附件列表（同时保留 local_path）
    attachments = []
    for att in parsed_result.get("attachment_links", []):
        from core_engine.scrapling_client import make_uuid
        attachments.append(
            AttachmentInfo(
                file_name=att.get("name", ""),
                file_url=att.get("url", ""),
                file_uuid=make_uuid(att.get("url", "")),
                local_path=att.get("local_path", ""),
            )
        )

    # 来源特有字段：从 parsed_result.extra 或 structured_data 中取
    # 站点字段在两条产出路径上位置不同：
    #   - CSS 抽取器（cde / nmpa）写在顶层 extra
    #   - API / fetcher 路径（clinicaltrials、fda_api_*）写在 structured_data.<字段>
    # 只取 extra 会让后者整批丢失（实测 18 条存量信封遗失 drug_name），故按白名单补齐。
    raw_extra = _merge_site_fields(
        parsed_result.get("extra") or structured.get("extra") or {}, structured, site
    )
    validated_extra = validate_extra(site, raw_extra)

    envelope = MetadataEnvelope(
        uuid=doc_uuid,
        title=parsed_result.get("title") or structured.get("title", ""),
        publish_date=parsed_result.get("publish_date") or structured.get("publish_date", ""),
        category=structured.get("category", ""),
        summary=structured.get("summary", ""),
        raw_content=parsed_result.get("markdown", ""),
        provenance=ProvenanceInfo(
            source_url=parsed_result.get("source_url", ""),
            source_site=site,
            local_file_path=parsed_result.get("local_path", ""),
            parent_uuid=parsed_result.get("parent_uuid", ""),
            crawl_timestamp=parsed_result.get("timestamp", ""),
        ),
        lineage=parsed_result.get("lineage", {}),
        # T3 对齐结果：当前只进信封（及 data/authority/ 旁路），尚未入库
        authority_resolutions=parsed_result.get("authority_resolutions") or {},
        identifier_resolutions=parsed_result.get("identifier_resolutions") or {},
        confidence=ConfidenceInfo(
            score=confidence_result["score"],
            reason=confidence_result["reason"],
        ),
        attachments=attachments,
        extra=validated_extra,
        parse_method=parsed_result.get("parse_method", ""),
        status=status,
        doc_type=parsed_result.get("doc_type", ""),
        language=parsed_result.get("language", ""),
    )

    # ── T3 对齐统计（纯计算，无副作用；结果落盘见下方「写库成功后」）──
    authority_stat = summarize(parsed_result.get("authority_resolutions") or {})
    identifier_stat = summarize(parsed_result.get("identifier_resolutions") or {})

    # ── 文档级结构化校验（T5：落库前契约检查 + 违规计数，非阻断）──
    doc_report = validate_document(envelope.model_dump())
    if doc_report.violation_count > 0:
        logger.warning(
            f"[Storage] 文档级校验发现 {doc_report.violation_count} 处违规: {doc_uuid}"
        )
        for _v in doc_report.violations:
            logger.warning(f"  - {_v['field']} [{_v['rule']}]: {_v['message']}")

    # ── 核心：写入数据库 ──
    success = True
    publication = {}
    try:
        result = upsert_document(envelope)
        if result is False:
            raise RuntimeError("文档写库未确认成功")
        publication = result if isinstance(result, dict) else {}
        logger.info(f"[Storage] 数据落库成功: {doc_uuid}")
    except Exception as e:
        logger.error(f"[Storage] 数据落库失败: {doc_uuid} | {e}")
        success = False

    # ── 落盘：字段级血缘 + T3 对齐结果（均在写库成功后执行）──
    if success:
        try:
            save_lineage(parsed_result)
        except Exception as e:
            logger.warning(f"[Storage] 血缘落盘失败（不影响主流程）: {doc_uuid} | {e}")

        # T3 对齐结果旁路存档（data/authority/<site>/<uuid>.json）
        # 建表前的过渡载体；upsert_document 不读该字段，故不影响落库结果
        try:
            save_authority_resolutions(parsed_result)
        except Exception as e:
            logger.warning(f"[Storage] 对齐结果存档失败（不影响主流程）: {doc_uuid} | {e}")

        # ── T3 对齐结果入库（A1，表见 V020）──
        # 与上方文件存档并存：文件是可复查快照，表是查询/统计/论文取数的正式载体。
        #
        # 内容变化转版本分支必须跳过 —— 此时 upsert_document 并未更新 documents 表
        # （旧正文继续挂旧抽取结果，新正文只进 publication_outbox），若照常写入，
        # 新正文的对齐结果会覆盖旧正文的记录，造成证据错配。
        if publication.get("legacy_cache_retained"):
            logger.warning(
                f"[Storage] 正文变化已转版本发布，跳过对齐结果入库以防覆盖旧版: {doc_uuid}"
            )
        else:
            try:
                written = upsert_authority_resolutions(
                    doc_uuid,
                    site,
                    parsed_result.get("authority_resolutions") or {},
                    parsed_result.get("identifier_resolutions") or {},
                )
                if written:
                    logger.info(f"[Storage] 对齐结果入库 {written} 行: {doc_uuid}")
            except Exception as e:
                logger.warning(f"[Storage] 对齐结果入库失败（不影响主流程）: {doc_uuid} | {e}")


    # ── 可选：本地文件存储（开发/调试用）──
    if success and APP.ENABLE_LOCAL_STORAGE:
        try:
            _write_to_local_storage(parsed_result, envelope, doc_uuid, site)
        except Exception as e:
            logger.warning(f"[Storage] 本地文件写入失败（不影响主流程）: {doc_uuid} | {e}")

    return {
        "uuid": doc_uuid,
        "status": status,
        "confidence_score": confidence_result["score"],
        "success": success,
        "violation_count": doc_report.violation_count,
        # T3 对齐统计（total=待对齐药名数，matched=命中数，match_rate=命中率）
        "authority_total": authority_stat["total"],
        "authority_matched": authority_stat["matched"],
        "authority_match_rate": authority_stat["match_rate"],
        # T3 标识符格式校验统计（与药名口径分开，勿合并计算）
        "identifier_total": identifier_stat["total"],
        "identifier_matched": identifier_stat["matched"],
        "identifier_match_rate": identifier_stat["match_rate"],
        # 服务器侧新增：写库产物（版本号 / 记录 id 等），放在最后以防覆盖上述统计
        **publication,
    }


def _write_to_local_storage(
    parsed_result: dict,
    envelope: MetadataEnvelope,
    doc_uuid: str,
    site: str,
) -> None:
    """
    可选：本地文件存储（仅用于开发/调试）。
    当 APP.ENABLE_LOCAL_STORAGE = True 时启用。
    """
    # Markdown 落盘
    md_dir = APP.MARKDOWN_DIR / site
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"{doc_uuid}.md"
    md_path.write_text(parsed_result.get("markdown", ""), encoding="utf-8")
    logger.debug(f"[Storage] Markdown 本地存储: {md_path}")

    # JSON 信封落盘
    json_dir = APP.JSON_DIR / site
    json_dir.mkdir(parents=True, exist_ok=True)
    json_path = json_dir / f"{doc_uuid}.json"
    json_path.write_text(
        envelope.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    logger.debug(f"[Storage] JSON 本地存储: {json_path}")

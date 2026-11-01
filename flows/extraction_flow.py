"""
实体抽取流程：已知结构化来源按原字段映射，叙述来源沿用 LLM

流程：
  1. 从 documents 表取 extraction_status=queued 的文档
  2. 智能分块（文章型按标题/表格型按行），带记忆上下文
  3. 逐块调 LLM → 解析 JSON → Pydantic 校验
  4. 跨块实体去重合并，关系下标重映射
  5. 消歧（6 阶段流水线：规范化→强ID→别名→规则→向量→LLM精排）
  6. insert_extraction_result 写库
  7. 更新 extraction_status = done / failed

调用方式：
  - 直接运行：python -m flows.extraction_flow [limit]
  - 被 API 触发：from flows.extraction_flow import run_extraction_for_uuids
"""
import time
from loguru import logger
from sqlalchemy import text

from biz_rules.extraction_prompts import get_extraction_prompt, VALID_RELATION_TYPES
from biz_rules.entity_schemas import validate_entity_data, VALID_ENTITY_TYPES
from biz_rules.native_extraction import NATIVE_SOURCES, RULE_VERSION, NativeExtractionError, extract_native_entities
from disambiguation.models import (
    CandidateEntity, DisambiguationResult, STATUS_AUTO_MATCHED,
    STATUS_CANDIDATE_MATCHED, STATUS_AMBIGUOUS, STATUS_NEW_ENTITY,
)
from core_engine.cloud_api_client import qwen_extract_structured
from flows.chunking import split_document, build_memory_context
from db.repository import (
    update_extraction_status,
    insert_extraction_result,
    lookup_alias_candidates,
    lookup_external_id_candidates,
)
from db.session import get_session
from db.models import Document
from config.settings import APP

ENTITY_EXTRACTION_SYSTEM_PROMPT = (
    "你是医药行业知识图谱构建专家。"
    "请严格按照用户提供的 JSON 格式返回抽取结果，不要添加任何额外文字或解释。"
    "只抽取文档中明确提及的信息，不要推断或编造。"
)


# ═══════════════════════════════════════════════════════════════════════
# 单块 LLM 调用
# ═══════════════════════════════════════════════════════════════════════

def _build_chunk_prompt(chunk, source_site: str) -> str:
    """
    为单个 Chunk 构建 prompt，注入记忆上下文和表格表头。
    """
    # 表格型：在正文前加表头说明
    if chunk.is_table and chunk.table_header:
        text = f"【表头】\n{chunk.table_header}\n\n【数据行】\n{chunk.text}"
    else:
        text = chunk.text

    # 多块时注入进度和记忆上下文
    prefix = ""
    if chunk.total_chunks > 1:
        prefix = f"【当前进度】第 {chunk.chunk_index + 1} 块 / 共 {chunk.total_chunks} 块\n"
        if chunk.memory_context:
            prefix += (
                f"\n【前序块已识别的实体（供参考，可建立关系）】\n"
                f"{chunk.memory_context}\n"
            )
        prefix += "\n"

    return get_extraction_prompt(source_site, prefix + text)


def _call_llm_for_chunk(chunk, source_site: str) -> dict:
    """调用 LLM 处理单块，返回原始 dict，失败返回 {}。"""
    prompt = _build_chunk_prompt(chunk, source_site)
    try:
        result = qwen_extract_structured.fn(
            prompt=prompt,
            system_prompt=ENTITY_EXTRACTION_SYSTEM_PROMPT,
            use_extraction_model=True,
            stream=False,
        )
        return result or {}
    except Exception as e:
        logger.error(f"[Extraction] LLM 调用失败 chunk={chunk.chunk_index}: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════
# 解析与校验
# ═══════════════════════════════════════════════════════════════════════

def _model_data(value):
    """Model content cannot supply internal evidence, identity or cache metadata."""
    if isinstance(value, dict):
        return {key: _model_data(item) for key, item in value.items()
                if isinstance(key, str) and not key.startswith("_")}
    if isinstance(value, list):
        return [_model_data(item) for item in value]
    return value


def _parse_chunk_output(raw: dict, chunk_index: int) -> tuple[list[dict], list[dict]]:
    """
    解析单块 LLM 输出，返回 (entities, relations)。
    relations 的 from/to 下标是块内局部下标，调用方负责重映射到全局下标。
    """
    raw_entities = raw.get("entities", [])
    raw_relations = raw.get("relations", [])

    if not isinstance(raw_entities, list):
        raw_entities = []
    if not isinstance(raw_relations, list):
        raw_relations = []

    entities: list[dict] = []
    for item in raw_entities:
        if not isinstance(item, dict):
            continue
        entity_type = item.get("entity_type", "")
        if entity_type not in VALID_ENTITY_TYPES:
            logger.warning(f"[Extraction] chunk={chunk_index} 未知实体类型 {entity_type!r}，跳过")
            continue
        entities.append({
            "entity_type": entity_type,
            "entity_name": str(item.get("entity_name", "")),
            "entity_data": validate_entity_data(entity_type, _model_data(item.get("entity_data", {}))),
            "source_text": str(item.get("source_text", ""))[:500],
            "confidence_score": float(item.get("confidence_score", 0.5)),
            "confidence_reason": str(item.get("confidence_reason", "")),
            "dict_id": None,
        })

    n = len(entities)
    relations: list[dict] = []
    for item in raw_relations:
        if not isinstance(item, dict):
            continue
        relation_type = item.get("relation_type", "")
        if relation_type not in VALID_RELATION_TYPES:
            continue
        from_idx = item.get("from_entity_idx")
        to_idx = item.get("to_entity_idx")
        if not isinstance(from_idx, int) or not isinstance(to_idx, int):
            continue
        if from_idx < 0 or from_idx >= n or to_idx < 0 or to_idx >= n:
            logger.warning(f"[Extraction] chunk={chunk_index} 关系下标越界，跳过")
            continue
        relations.append({
            "from_entity_idx": from_idx,   # 块内局部下标，待重映射
            "to_entity_idx": to_idx,
            "relation_type": relation_type,
            "relation_props": _model_data(item.get("relation_props", {})),
            "source_text": str(item.get("source_text", ""))[:500],
            "confidence_score": float(item.get("confidence_score", 0.5)),
            "confidence_reason": str(item.get("confidence_reason", "")),
        })

    return entities, relations


# ═══════════════════════════════════════════════════════════════════════
# 跨块合并
# ═══════════════════════════════════════════════════════════════════════

def _merge_chunks(
    all_entities_per_chunk: list[list[dict]],
    all_relations_per_chunk: list[list[dict]],
) -> tuple[list[dict], list[dict]]:
    """
    合并所有块的实体和关系。
    - 实体去重，建立旧下标 → 新下标的映射
    - 关系的 from/to 下标重映射到合并后的全局下标
    """
    # 1. 展平所有实体，记录每个实体在合并前的全局位置
    flat_entities: list[dict] = []
    # chunk_offset[i] = 第 i 块实体在 flat_entities 中的起始下标
    chunk_offsets: list[int] = []
    for chunk_ents in all_entities_per_chunk:
        chunk_offsets.append(len(flat_entities))
        flat_entities.extend(chunk_ents)

    # 2. 去重，建立 flat_index → dedup_index 映射
    dedup_map: dict[int, int] = {}   # flat_index → 去重后的下标
    seen: dict[tuple, int] = {}      # (type, name) → dedup_index
    deduped: list[dict] = []

    for flat_idx, ent in enumerate(flat_entities):
        key = (ent["entity_type"], ent["entity_name"].strip())
        if not key[1]:
            continue
        if key in seen:
            # 已存在，更新置信度（取最高）
            existing_idx = seen[key]
            if ent["confidence_score"] > deduped[existing_idx]["confidence_score"]:
                deduped[existing_idx] = ent
            dedup_map[flat_idx] = existing_idx
        else:
            new_idx = len(deduped)
            deduped.append(ent)
            seen[key] = new_idx
            dedup_map[flat_idx] = new_idx

    # 3. 关系重映射
    merged_relations: list[dict] = []
    seen_relations: set[tuple] = set()

    for chunk_idx, chunk_rels in enumerate(all_relations_per_chunk):
        offset = chunk_offsets[chunk_idx]
        for rel in chunk_rels:
            flat_from = offset + rel["from_entity_idx"]
            flat_to = offset + rel["to_entity_idx"]
            new_from = dedup_map.get(flat_from)
            new_to = dedup_map.get(flat_to)
            if new_from is None or new_to is None:
                continue
            # 关系去重
            rel_key = (new_from, new_to, rel["relation_type"])
            if rel_key in seen_relations:
                continue
            seen_relations.add(rel_key)
            merged_relations.append({
                **rel,
                "from_entity_idx": new_from,
                "to_entity_idx": new_to,
            })

    return deduped, merged_relations


# ═══════════════════════════════════════════════════════════════════════
# 消歧
# ═══════════════════════════════════════════════════════════════════════

_NATIVE_IDENTIFIERS = {
    "clinicaltrials.NCT": ("ClinicalTrial", "trial_id", {"clinicaltrials"}),
    "fda.application_number": ("RegulatoryApplication", "application_number", {"fda_crl", "fda_api_drug_label"}),
    "fda.recall_number": ("RecallProduct", "recall_number", {"fda_api_enforcement"}),
}


def _native_identifier(entity, *, native_source=None):
    data = entity.get("entity_data") or {}
    native = data.get("_native") or {}
    kind, key, sources = _NATIVE_IDENTIFIERS.get(native.get("identity_namespace"), (None, None, set()))
    if (native_source in sources and entity.get("source_site") == native_source
            and native.get("rule_version") == RULE_VERSION and kind == entity.get("entity_type")
            and native.get("identity_value") and data.get(key) == native["identity_value"]):
        return key, native["identity_value"]
    return None


def _disambiguate(entities: list[dict], session=None, *, native_source=None) -> list[dict]:
    """Typed native IDs use exact lookup; named endpoints use the existing pipeline."""
    from disambiguation import disambiguate_entities
    named = []
    for entity in entities:
        identifier = _native_identifier(entity, native_source=native_source)
        if not identifier:
            named.append(entity)
            continue
        key, value = identifier
        matches = lookup_external_id_candidates(entity["entity_type"], key, value, session=session)
        matches = list({match["dict_id"]: match for match in matches}.values())
        unique = len(matches) == 1
        result = DisambiguationResult(
            status=STATUS_AUTO_MATCHED if unique else STATUS_AMBIGUOUS if matches else STATUS_NEW_ENTITY,
            dict_id=matches[0]["dict_id"] if unique else None, auto_link=unique,
            confidence=1.0 if unique else 0.0,
            reason=f"native typed identifier lookup: {len(matches)} candidates; not a human verification",
            match_step="step1_native_id", rule_version=RULE_VERSION,
            candidates=[CandidateEntity(dict_id=match["dict_id"],
                name=match.get("canonical_name") or entity["entity_name"], match_type="native_id",
                canonical_data=match.get("canonical_data") or {}, external_ids=match.get("external_ids") or {})
                for match in matches],
        )
        entity["dict_id"] = result.dict_id
        entity["_disambig"] = result.to_metadata()
        entity["entity_data"]["_disambig"] = entity["_disambig"]
        entity["confidence_score"] = result.confidence
        entity["confidence_reason"] = result.reason
    if named:
        disambiguate_entities(named, session=session)
    return entities


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════

# 全局写库串行锁 key（pg_advisory_xact_lock）。
# 跨进程有效，确保同一时刻只有一个 Worker 执行"复核 + 写库"临界区。
# 事务提交时自动释放，无需手动 unlock。
_EXTRACTION_WRITE_LOCK_KEY = 0x7A4B3C2D


def extract_single_document(
    doc_uuid: str,
    raw_content: str,
    source_site: str,
    doc_type: str,
    *,
    source_version_id: str | None = None,
    source_ref: dict | None = None,
) -> bool:
    """
    对单篇文档执行完整抽取流程（支持长文档多块带记忆）。
    返回 True 表示成功，False 表示失败。
    """
    logger.info(f"[Extraction] 开始抽取: {doc_uuid} site={source_site}")

    if not raw_content or not raw_content.strip():
        logger.warning(f"[Extraction] 文档内容为空，跳过: {doc_uuid}")
        update_extraction_status(doc_uuid, "failed", error="文档raw_content为空，无法抽取")
        return False

    native = source_site in NATIVE_SOURCES
    if native:
        try:
            projection = extract_native_entities(raw_content, source_site,
                                                 source_version_id=source_version_id,
                                                 source_ref=source_ref)
            entities, relations = projection["entities"], projection["relations"]
        except NativeExtractionError as error:
            update_extraction_status(doc_uuid, "failed", model=RULE_VERSION,
                                     error=f"原始结构化资料不可用，未调用实体抽取LLM: {error}")
            return False
    else:
        # 1. 智能分块
        chunks = split_document(raw_content)
        if not chunks:
            logger.warning(f"[Extraction] 文档内容为空，跳过: {doc_uuid}")
            update_extraction_status(doc_uuid, "failed", error="文档内容为空")
            return False

        logger.info(f"[Extraction] 分块完成: {doc_uuid} 共 {len(chunks)} 块")

        # 2. 逐块抽取，携带记忆上下文
        all_entities_per_chunk: list[list[dict]] = []
        all_relations_per_chunk: list[list[dict]] = []
        accumulated_entities: list[dict] = []   # 用于生成记忆摘要
        llm_failed_chunks = 0                    # LLM 调用失败（返回空）的块数

        for chunk in chunks:
            # 注入记忆上下文
            chunk.memory_context = build_memory_context(accumulated_entities)

            if len(chunks) > 1:
                logger.info(
                    f"[Extraction] 处理第 {chunk.chunk_index + 1}/{chunk.total_chunks} 块"
                    f"（{'表格' if chunk.is_table else '文章'}型，{len(chunk.text)} 字）"
                )

            raw_result = _call_llm_for_chunk(chunk, source_site)
            if not raw_result:
                logger.warning(f"[Extraction] 第 {chunk.chunk_index + 1} 块 LLM 返回空，跳过")
                llm_failed_chunks += 1
                all_entities_per_chunk.append([])
                all_relations_per_chunk.append([])
                continue

            chunk_entities, chunk_relations = _parse_chunk_output(raw_result, chunk.chunk_index)
            all_entities_per_chunk.append(chunk_entities)
            all_relations_per_chunk.append(chunk_relations)
            accumulated_entities.extend(chunk_entities)

            logger.info(
                f"[Extraction] 第 {chunk.chunk_index + 1} 块完成: "
                f"entities={len(chunk_entities)} relations={len(chunk_relations)}"
            )

        # 3. 跨块合并去重
        entities, relations = _merge_chunks(all_entities_per_chunk, all_relations_per_chunk)
        logger.info(
            f"[Extraction] 合并完成: {doc_uuid} "
            f"entities={len(entities)} relations={len(relations)}"
        )

        if not entities:
            if llm_failed_chunks >= len(chunks):
                # LLM 完全不可用 → 真失败
                error = "LLM 不可用（所有块调用失败/返回空）"
                logger.warning(f"[Extraction] {error}: {doc_uuid}")
                update_extraction_status(doc_uuid, "failed", error=error)
                return False
            # LLM 正常响应但文档中无目标实体（如化妆品/器械文档，Drug 边界收紧后正确抽空）
            # 与 failed 区分：这是「抽取成功但无需求实体」，不是失败，单独入 no_entity
            error = "抽取成功但无目标实体"
            logger.info(f"[Extraction] {error}: {doc_uuid}（no_entity）")
            update_extraction_status(doc_uuid, "no_entity", error=error)
            return True

    # 4. 消歧（DB 事务内但锁外执行，可并行，可包含慢 API 调用）
    for ent in entities:
        ent["source_site"] = source_site
        if source_site.startswith(("nmpa", "cde")):
            ent.setdefault("entity_data", {})["_native"] = {
                "rule_version": RULE_VERSION, "identity_status": "unresolved",
                "limitations": ["未核对真实网页固定编号标签；模型抽取编号仅作弱候选，不作为规则确认身份"],
            }

    try:
        with get_session() as session:
            if session is None:
                logger.error(f"[Extraction] DB 未连接，无法入库: {doc_uuid}")
                return False

            # 4a. 消歧 — 锁外，多进程可并行
            disambig_start = time.time()
            entities = _disambiguate(entities, session=session, native_source=source_site if native else None)
            if source_site.startswith(("nmpa", "cde")):
                for ent in entities:
                    info = dict(ent.get("_disambig") or (ent.get("entity_data") or {}).get("_disambig")
                                or DisambiguationResult(status=STATUS_AMBIGUOUS).to_metadata())
                    selected = info.get("dict_id") or ent.get("dict_id")
                    candidates = list(info.get("candidates") or [])
                    if selected and not any(candidate["dict_id"] == selected for candidate in candidates):
                        candidates.append({"dict_id": selected, "name": ent.get("entity_name", ""),
                                           "similarity_score": ent.get("confidence_score", 0.0),
                                           "match_type": "unverified_page_identifier"})
                    info.update(status=STATUS_CANDIDATE_MATCHED if selected else STATUS_AMBIGUOUS,
                                dict_id=selected, candidates=candidates, auto_link=False,
                                reason="source page identifier labels are unverified; requires review")
                    ent["dict_id"] = None
                    ent["_disambig"] = info
                    ent.setdefault("entity_data", {})["_disambig"] = info
            disambig_sec = time.time() - disambig_start
            logger.info(
                f"[Extraction] 消歧完成: doc={doc_uuid} "
                f"elapsed={disambig_sec:.2f}s entities={len(entities)}"
            )

            # 4a2. 锁外预生成 embedding — 对消歧判 new 的实体，提前调云 API
            #       embedding 缓存写入 entity_data["_embeddings"]，
            #       锁内 _write_aliases_with_embedding 直接复用，不占锁
            from core_engine.cloud_api_client import generate_embedding
            embed_start = time.time()
            embed_count = 0
            for ent in entities:
                if _native_identifier(ent, native_source=source_site if native else None):
                    continue
                ed = ent.get("entity_data", {}) or {}
                entity_name = (ent.get("entity_name") or "").strip()
                names: set[str] = {entity_name} if entity_name else set()
                data_name = (ed.get("name") or "").strip()
                if data_name:
                    names.add(data_name)
                data_name_en = (ed.get("name_en") or "").strip()
                if data_name_en:
                    names.add(data_name_en)
                for a in (ed.get("aliases") or []):
                    s = (str(a) or "").strip()
                    if s:
                        names.add(s)
                names.discard("")
                if not names:
                    continue
                cache: dict[str, list[float]] = {}
                for name in names:
                    try:
                        emb = generate_embedding(name)
                    except Exception:
                        emb = None
                    if emb:
                        cache[name] = emb
                        embed_count += 1
                if cache:
                    ed["_embeddings"] = cache
                    ent["entity_data"] = ed
            embed_sec = time.time() - embed_start
            if embed_count:
                logger.info(
                    f"[Extraction] 预生成 embedding: doc={doc_uuid} "
                    f"count={embed_count} elapsed={embed_sec:.2f}s"
                )

            # 4b. 获取全局写库串行锁（pg_advisory_xact_lock，跨进程有效，
            #     事务提交时自动释放）
            lock_wait_start = time.time()
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _EXTRACTION_WRITE_LOCK_KEY},
            )
            lock_wait_sec = time.time() - lock_wait_start
            if lock_wait_sec > 0.1:
                logger.warning(
                    f"[Extraction] advisory lock 等待: doc={doc_uuid} "
                    f"wait={lock_wait_sec:.2f}s"
                )
            lock_hold_start = time.time()
            logger.info(f"[Extraction] 进入写库临界区: doc={doc_uuid}")

            # 4c. 快速复核：对消歧判为 new 的实体，重跑 Step 2 alias lookup。
            #     解决多进程竞态：消歧时其他进程的 alias 尚未 commit，
            #     此时已可见，命中则修正 dict_id。
            for ent in entities:
                if ent.get("dict_id") is not None:
                    continue
                name, kind = ent.get("entity_name", ""), ent.get("entity_type", "")
                if not name or not kind:
                    continue
                matches = lookup_alias_candidates(name, kind, session=session)
                if not matches:
                    continue
                info = ent.get("_disambig") or ent.setdefault("entity_data", {}).get("_disambig")
                info = dict(info or DisambiguationResult(status=STATUS_AMBIGUOUS).to_metadata())
                candidates = {candidate["dict_id"]: candidate for candidate in info.get("candidates", [])}
                for match in matches:
                    candidates.setdefault(match["dict_id"], {"dict_id": match["dict_id"],
                        "name": match.get("canonical_name") or name, "similarity_score": 0.85,
                        "match_type": "alias_recheck", "canonical_data": match.get("canonical_data") or {},
                        "aliases": [name], "external_ids": match.get("external_ids") or {}})
                info.update(status=STATUS_CANDIDATE_MATCHED if len(candidates) == 1 else STATUS_AMBIGUOUS,
                            dict_id=next(iter(candidates)) if len(candidates) == 1 else None,
                            candidates=list(candidates.values()), match_step="step2_alias_recheck",
                            auto_link=False, reason="concurrent alias match requires review")
                ent["_disambig"] = info
                ent.setdefault("entity_data", {})["_disambig"] = info

            # 4d. 清理消歧临时字段
            for ent in entities:
                disambig = ent.pop("_disambig", None)
                if disambig:
                    ent.setdefault("entity_data", {})["_disambig"] = disambig
                ent.pop("source_site", None)
                # 清理预生成 embedding 缓存（insert 路径已在 _write_aliases_with_embedding 中 pop）
                ent.setdefault("entity_data", {}).pop("_embeddings", None)

            # 4e. 入库（alias + embedding 已在锁外预生成，锁内直接写）
            insert_extraction_result(
                source_doc_uuid=doc_uuid,
                source_site=source_site,
                entities=entities,
                relations=relations,
                session=session,
            )

            lock_hold_sec = time.time() - lock_hold_start
            logger.info(
                f"[Extraction] 离开写库临界区: doc={doc_uuid} "
                f"hold={lock_hold_sec:.2f}s"
            )
        # session commit → advisory lock 释放

    except Exception as e:
        logger.error(f"[Extraction] 写库失败: {doc_uuid} | {e}")
        update_extraction_status(doc_uuid, "failed", error=f"写库失败: {e}")
        return False

    if native:
        update_extraction_status(doc_uuid, "done", model=RULE_VERSION)
    logger.info(f"[Extraction] 完成: {doc_uuid}")
    return True


# ═══════════════════════════════════════════════════════════════════════
# 批量入口
# ═══════════════════════════════════════════════════════════════════════

def run_extraction_for_uuids(uuids: list[str]) -> dict:
    """
    对指定 UUID 列表提交抽取任务到 WorkerManager。
    队列只存 uuid，worker 执行时按需查 DB 取内容。
    被 API 触发时调用。
    """
    from flows.extraction_worker import ExtractionTask, get_extraction_manager

    with get_session() as session:
        if session is None:
            return {"submitted": 0, "failed": len(uuids), "error": "DB未连接"}
        # 校验 uuid 是否存在，不预载内容
        existing = {
            r.uuid
            for r in session.query(Document.uuid).filter(
                Document.uuid.in_(uuids),
                Document.is_deleted == False,
            ).all()
        }

    manager = get_extraction_manager()
    submitted = 0
    failed = 0

    for uuid in uuids:
        if uuid not in existing:
            logger.warning(f"[Extraction] 文档不存在: {uuid}")
            failed += 1
            continue

        task = ExtractionTask(uuid=uuid)
        manager.submit(task)
        submitted += 1

    logger.info(
        f"[Extraction] 已提交 {submitted} 篇到 WorkerManager "
        f"(workers={manager.num_workers}, pending={manager.total_pending()})"
    )
    return {"submitted": submitted, "failed": failed}


def run_extraction_batch(limit: int = 10) -> dict:
    """
    批量处理 extraction_status=queued 的文档，提交到 WorkerManager。
    作为定时任务恢复机制：API 标记 queued 后如果进程崩溃，
    本函数将遗漏的 queued 文档重新入队。
    Worker 执行时按需查 DB 取内容，队列只存 uuid。
    """
    from flows.extraction_worker import ExtractionTask, get_extraction_manager

    with get_session() as session:
        if session is None:
            return {"submitted": 0, "failed": 0}
        rows = (
            session.query(Document.uuid)
            .filter(
                Document.extraction_status == "queued",
                Document.is_deleted == False,
            )
            .limit(limit)
            .all()
        )

    manager = get_extraction_manager()
    submitted = 0

    for (uuid,) in rows:
        task = ExtractionTask(uuid=uuid)
        manager.submit(task)
        submitted += 1

    logger.info(
        f"[Extraction] 批量提交 {submitted} 篇到 WorkerManager "
        f"(workers={manager.num_workers}, pending={manager.total_pending()})"
    )
    return {"submitted": submitted}


if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    result = run_extraction_batch(limit=limit)
    print(f"抽取提交完成: submitted={result['submitted']}")

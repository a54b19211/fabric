"""
LLM 实体抽取流程

流程：
  1. 从 documents 表取 extraction_status=processing 的文档
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
from loguru import logger

from biz_rules.extraction_prompts import get_extraction_prompt, VALID_RELATION_TYPES
from biz_rules.entity_schemas import validate_entity_data, VALID_ENTITY_TYPES
from core_engine.cloud_api_client import qwen_extract_structured
from flows.chunking import split_document, build_memory_context
from db.repository import (
    update_extraction_status,
    insert_extraction_result,
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
            stream=True,
        )
        return result or {}
    except Exception as e:
        logger.error(f"[Extraction] LLM 调用失败 chunk={chunk.chunk_index}: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════
# 解析与校验
# ═══════════════════════════════════════════════════════════════════════

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
            "entity_data": validate_entity_data(entity_type, item.get("entity_data", {})),
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
            "relation_props": item.get("relation_props", {}),
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

def _disambiguate(entities: list[dict]) -> list[dict]:
    """完整 6 阶段消歧流水线（替换旧的仅精确别名匹配）。"""
    from disambiguation import disambiguate_entities
    return disambiguate_entities(entities)


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════

def extract_single_document(
    doc_uuid: str,
    raw_content: str,
    source_site: str,
    doc_type: str,
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
        logger.warning(f"[Extraction] 未抽取到任何实体: {doc_uuid}")
        update_extraction_status(doc_uuid, "done", model=APP.EXTRACTION_MODEL_NAME)
        return True  # 不算失败，文档可能确实没有可抽取的实体

    # 4. 消歧（注入 source_site 上下文）
    for ent in entities:
        ent["source_site"] = source_site
    entities = _disambiguate(entities)

    # 将消歧元数据合并到 entity_data，并清理临时字段
    for ent in entities:
        disambig = ent.pop("_disambig", None)
        if disambig:
            ent.setdefault("entity_data", {})["_disambig"] = disambig
        ent.pop("source_site", None)

    # 5. 写库
    try:
        insert_extraction_result(
            source_doc_uuid=doc_uuid,
            source_site=source_site,
            entities=entities,
            relations=relations,
        )
    except Exception as e:
        logger.error(f"[Extraction] 写库失败: {doc_uuid} | {e}")
        update_extraction_status(doc_uuid, "failed", error=f"写库失败: {e}")
        return False

    update_extraction_status(doc_uuid, "done", model=APP.EXTRACTION_MODEL_NAME)
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
    批量处理 extraction_status=processing 的文档，提交到 WorkerManager。
    Worker 执行时按需查 DB 取内容，队列只存 uuid。
    被定时任务调用。
    """
    from flows.extraction_worker import ExtractionTask, get_extraction_manager

    with get_session() as session:
        if session is None:
            return {"submitted": 0, "failed": 0}
        rows = (
            session.query(Document.uuid)
            .filter(
                Document.extraction_status == "processing",
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

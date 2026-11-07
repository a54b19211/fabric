"""Entity dictionary maintenance: soft deletes and candidate dictionary lookup for review."""

from sqlalchemy import false
from loguru import logger

from db.models import EntityAlias, EntityDict

from db.repository.audit import _write_operation_log


def soft_delete_entity_dict(dict_id: int, operator: str = "") -> None:
    """
    软删除 entity_dict 及其所有别名。
    PrimeKG 来源（canonical_source='primekg'）禁止删除。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        ent = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
        if ent is None:
            logger.warning(f"[DB] soft_delete_entity_dict: dict_id={dict_id} 不存在")
            return
        if ent.canonical_source == "primekg":
            logger.warning(
                f"[DB] soft_delete_entity_dict: dict_id={dict_id} 来源为 PrimeKG，禁止删除"
            )
            raise ValueError(f"无法删除 PrimeKG 来源的实体 (dict_id={dict_id})")

        ent.is_deleted = True
        session.query(EntityAlias).filter(
            EntityAlias.dict_id == dict_id,
            EntityAlias.is_deleted == false(),
        ).update({"is_deleted": True}, synchronize_session=False)

        _write_operation_log(
            session, "update", "entity_dict", dict_id,
            business_action="delete",
            description=f"软删除 entity_dict: type={ent.entity_type} operator={operator}",
            details={"dict_id": dict_id, "entity_type": ent.entity_type, "canonical_source": ent.canonical_source,
                      "operator": operator},
            operator=operator,
            trigger_source="soft_delete_entity_dict",
        )
        logger.info(
            f"[DB] soft_delete_entity_dict: dict_id={dict_id} "
            f"entity_type={ent.entity_type} operator={operator}"
        )


def soft_delete_entity_alias(alias_id: int, operator: str = "") -> None:
    """
    软删除单个别名。
    若所属 entity_dict 来源为 PrimeKg，禁止删除。
    若别名是 canonical 名称，禁止删除（标准名不可删）。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        alias = session.query(EntityAlias).filter(EntityAlias.id == alias_id).first()
        if alias is None:
            logger.warning(f"[DB] soft_delete_entity_alias: alias_id={alias_id} 不存在")
            return
        if alias.is_deleted:
            return

        parent = session.query(EntityDict).filter(EntityDict.id == alias.dict_id).first()
        if parent is None:
            logger.warning(f"[DB] soft_delete_entity_alias: 所属 entity_dict 不存在 dict_id={alias.dict_id}")
            return
        if parent.canonical_source == "primekg":
            logger.warning(
                f"[DB] soft_delete_entity_alias: alias_id={alias_id} 所属实体来源为 PrimeKG，禁止删除"
            )
            raise ValueError(f"无法删除 PrimeKG 来源实体的别名 (alias_id={alias_id})")

        if alias.is_canonical:
            logger.warning(
                f"[DB] soft_delete_entity_alias: alias_id={alias_id} "
                f"'{alias.name}' 为标准名称，禁止删除"
            )
            raise ValueError(f"无法删除实体标准名称 '{alias.name}' (alias_id={alias_id})")

        alias.is_deleted = True
        _write_operation_log(
            session, "update", "entity_aliases", alias_id,
            business_action="delete",
            description=f"软删除别名: '{alias.name}' dict_id={alias.dict_id} operator={operator}",
            details={"alias_id": alias_id, "name": alias.name, "dict_id": alias.dict_id,
                      "operator": operator},
            operator=operator,
            trigger_source="soft_delete_entity_alias",
        )
        logger.info(
            f"[DB] soft_delete_entity_alias: alias_id={alias_id} "
            f"'{alias.name}' dict_id={alias.dict_id} operator={operator}"
        )


def find_candidate_dicts_for_review(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
    top_k: int = 10,
) -> list[dict]:
    """
    审核页实时查询 entity_dict 中与给定实体相似的候选项。
    不过滤 is_verified（未审核的 entity_dict 也要展示给审核员）。

    查询路径：
      1. entity_aliases 精确匹配
      2. entity_aliases name_embedding 向量召回
      3. BGE-Reranker 精排

    返回：[{dict_id, entity_type, name, aliases, canonical_source,
            is_verified, match_type, score}, ...]
    """
    from core_engine.cloud_api_client import generate_embedding
    from db.session import get_session

    entity_data = entity_data or {}

    with get_session() as session:
        if session is None:
            return []

        raw_results: dict[int, dict] = {}  # dict_id → {dict_id, name, match_type}

        # 1. 别名精确匹配
        exact_rows = (
            session.query(EntityAlias.dict_id, EntityAlias.name)
            .filter(
                EntityAlias.name == entity_name.strip(),
                EntityAlias.is_deleted == false(),
            )
            .all()
        )
        for row in exact_rows:
            raw_results[row.dict_id] = {
                "dict_id": row.dict_id,
                "name": row.name,
                "match_type": "exact_alias",
            }

        # 2. 向量召回（top_k * 2 给 reranker 留余量）
        embedding = generate_embedding(entity_name)
        if embedding:
            try:
                dist = EntityAlias.name_embedding.cosine_distance(embedding)
                vector_rows = (
                    session.query(EntityAlias.dict_id, EntityAlias.name)
                    .join(EntityDict, EntityAlias.dict_id == EntityDict.id)
                    .filter(
                        EntityAlias.is_deleted == false(),
                        EntityDict.is_deleted == false(),
                        EntityDict.entity_type == entity_type,
                        EntityAlias.name_embedding.isnot(None),
                    )
                    .order_by(dist)
                    .limit(top_k * 2)
                    .all()
                )
                for row in vector_rows:
                    if row.dict_id not in raw_results:
                        raw_results[row.dict_id] = {
                            "dict_id": row.dict_id,
                            "name": row.name,
                            "match_type": "vector",
                        }
            except Exception as e:
                logger.warning(f"[DB] find_candidate_dicts_for_review 向量搜索失败: {e}")

        if not raw_results:
            return []

        # 3. 批量查 entity_dict 和 aliases
        all_dict_ids = list(raw_results.keys())
        dict_rows = (
            session.query(EntityDict)
            .filter(EntityDict.id.in_(all_dict_ids))
            .all()
        )
        dict_map = {d.id: d for d in dict_rows}

        alias_rows = (
            session.query(EntityAlias.dict_id, EntityAlias.name)
            .filter(
                EntityAlias.dict_id.in_(all_dict_ids),
                EntityAlias.is_deleted == false(),
            )
            .all()
        )
        aliases_by_dict: dict[int, list[str]] = {}
        for did, name in alias_rows:
            aliases_by_dict.setdefault(did, []).append(name)

        # 4. Reranker 精排
        rerank_scores = _rerank_candidates(entity_name, entity_data, raw_results, dict_map, aliases_by_dict)

        # 5. 组装结果，按 reranker 分数降序
        output = []
        for dict_id, r in raw_results.items():
            d = dict_map.get(dict_id)
            if d is None:
                continue
            score = rerank_scores.get(dict_id)
            if score is None:
                continue  # reranker 未返回分数，跳过
            output.append({
                "dict_id": dict_id,
                "entity_type": d.entity_type,
                "name": r["name"],
                "aliases": aliases_by_dict.get(dict_id, []),
                "canonical_source": d.canonical_source or "",
                "is_verified": d.is_verified,
                "match_type": r["match_type"],
                "score": round(score, 4),
            })

        output.sort(key=lambda x: x["score"], reverse=True)
        return output[:top_k]


def _rerank_candidates(
    entity_name: str,
    entity_data: dict,
    raw_results: dict[int, dict],
    dict_map: dict[int, EntityDict],
    aliases_by_dict: dict[int, list[str]],
) -> dict[int, float]:
    """
    调用 BGE-Reranker 对候选 entity_dict 精排，返回 {dict_id: score}。
    """
    import requests

    from config.settings import APP

    if not raw_results:
        return {}

    # 构造 query 文本（多语言名称拼接）
    query_parts = [entity_name]
    if entity_data:
        en = entity_data.get("name", "").strip()
        if en and en != entity_name:
            query_parts.append(en)
        en2 = entity_data.get("name_en", "").strip()
        if en2:
            query_parts.append(en2)
    query = " | ".join(query_parts)

    # 构造每个候选的 text（名称 + aliases）
    dict_ids_ordered = list(raw_results.keys())
    texts = []
    for did in dict_ids_ordered:
        parts = [raw_results[did]["name"]]
        d = dict_map.get(did)
        if d:
            cd = d.canonical_data or {}
            cd_name = cd.get("name", "").strip()
            if cd_name and cd_name != parts[0]:
                parts.append(cd_name)
            cd_name_en = cd.get("name_en", "").strip()
            if cd_name_en:
                parts.append(cd_name_en)
        for alias in aliases_by_dict.get(did, []):
            if alias not in parts:
                parts.append(alias)
        texts.append(" | ".join(parts))

    base = APP.RERANK_API_BASE.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    rerank_url = base + "/rerank"

    try:
        resp = requests.post(
            rerank_url,
            json={"query": query, "documents": texts},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            items = data
        else:
            items = data.get("results", [])
        scores: dict[int, float] = {}
        for item in items:
            idx = item.get("index")
            if idx is not None and 0 <= idx < len(dict_ids_ordered):
                scores[dict_ids_ordered[idx]] = item.get("relevance_score", 0.0)
        return scores
    except Exception as e:
        logger.warning(f"[DB] _rerank_candidates 失败: {e}")
        # 降级：用 match_type 给粗略分
        fallback: dict[int, float] = {}
        for did, r in raw_results.items():
            fallback[did] = 1.0 if r["match_type"] == "exact_alias" else 0.5
        return fallback

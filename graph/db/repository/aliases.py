"""Alias, external-id and similarity lookups; alias writing with embeddings."""

import re
from typing import Optional

from loguru import logger
from sqlalchemy import false, func, text

from graph.db.models import EntityAlias, EntityDict
from graph.db.repository.audit import _write_operation_log

# 制剂后缀：entity_name 以此结尾时，不写入 entity_name 的子串作为 alias
_FORMULATION_SUFFIX = re.compile(
    r'(片|颗粒|试剂|胶囊|注射液|分散片|缓释片|肠溶片|滴丸|口服液'
    r'|糖浆|软胶囊|咀嚼片|泡腾片|注射用|冻干|混悬液|滴眼液'
    r'|软膏|乳膏|凝胶|栓剂|气雾剂|喷雾剂|贴剂|洗剂|搽剂)$'
)


def lookup_alias_candidates(name: str, entity_type: str, session=None) -> list[dict]:
    """Return every live same-name dictionary; a homonym is never a unique match."""
    from graph.db.identity import use_session
    with use_session(session) as active:
        rows = active.query(EntityDict.id, EntityDict.canonical_data, EntityDict.external_ids).join(
            EntityAlias, EntityAlias.dict_id == EntityDict.id).filter(
                func.lower(EntityAlias.name) == name.strip().lower(),
                EntityDict.entity_type == entity_type, EntityDict.is_deleted == false(),
                EntityAlias.is_deleted == false()).distinct().order_by(EntityDict.id).all()
        return [{'dict_id': row.id, 'name': name, 'canonical_name': (row.canonical_data or {}).get('name', ''),
                 'canonical_data': row.canonical_data or {}, 'external_ids': row.external_ids or {}} for row in rows]


def lookup_alias(name: str, entity_type: str, session=None) -> Optional[int]:
    """Compatibility API. Return an ID only when all live same-name matches agree."""
    rows = lookup_alias_candidates(name, entity_type, session=session)
    identities = {row['dict_id'] for row in rows}
    return next(iter(identities)) if len(identities) == 1 else None


def search_similar_entities(
    embedding: list[float],
    entity_type: str,
    top_k: int = 10,
    session=None,
    strict_errors: bool = True,
) -> list[dict]:
    """
    向量相似度召回（pgvector，仅做候选召回，分数供参考不直接决策）。
    在 entity_aliases.name_embedding 上做余弦近邻搜索。
    返回 top_k 条，每条包含：dict_id, name, similarity_score, canonical_data, aliases
    session 可选：传入则复用，不传则自开（向后兼容）。
    """
    def _query(sess):
        try:
            dist = EntityAlias.name_embedding.cosine_distance(embedding)
            rows = (
                sess.query(
                    EntityAlias.dict_id,
                    EntityAlias.name,
                    (1 - dist).label("similarity_score"),
                    EntityDict.canonical_data,
                )
                .join(EntityDict, EntityAlias.dict_id == EntityDict.id)
                .filter(
                    EntityAlias.is_deleted == false(),
                    EntityDict.is_deleted == false(),
                    EntityDict.entity_type == entity_type,
                    EntityAlias.name_embedding.isnot(None),
                )
                .order_by(dist)
                .limit(top_k)
                .all()
            )
            all_dict_ids = [r.dict_id for r in rows]
            alias_rows = (
                sess.query(EntityAlias.dict_id, EntityAlias.name)
                .filter(
                    EntityAlias.dict_id.in_(all_dict_ids),
                    EntityAlias.is_deleted == false(),
                )
                .all()
            )
            aliases_by_dict: dict[int, list[str]] = {}
            for did, name in alias_rows:
                aliases_by_dict.setdefault(did, []).append(name)

            results = []
            for r in rows:
                canonical = r.canonical_data or {}
                results.append({
                    "dict_id": r.dict_id,
                    "name": r.name,
                    "similarity_score": float(r.similarity_score),
                    "canonical_data": canonical,
                    "aliases": aliases_by_dict.get(r.dict_id, []),
                })
            return results
        except Exception:
            logger.error('[DB] Entity similarity lookup unavailable')
            if strict_errors:
                raise RuntimeError('Entity similarity lookup unavailable') from None
            return []

    if session is not None:
        return _query(session)

    from graph.db.session import get_session
    with get_session() as sess:
        if sess is None:
            if strict_errors:
                raise RuntimeError("Entity similarity lookup is not configured")
            return []
        return _query(sess)


def add_aliases(dict_id: int, entity_type: str, aliases: list[tuple[str, str, bool]]) -> None:
    """
    添加别名（幂等，已存在则跳过）。
    aliases: [(name, lang, is_canonical), ...]
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        for name, lang, is_canonical in aliases:
            exists = (
                session.query(EntityAlias)
                .filter(EntityAlias.dict_id == dict_id, EntityAlias.name == name,
                        EntityAlias.is_deleted == false())
                .first()
            )
            if exists is None:
                alias = EntityAlias(
                    dict_id=dict_id,
                    name=name,
                    lang=lang or "",
                    is_canonical=bool(is_canonical),
                    entity_type=entity_type,
                )
                session.add(alias)
                session.flush()
                _write_operation_log(
                    session, "insert", "entity_aliases", alias.id,
                    business_action="alias_sync",
                    description=f"添加别名: '{name}' dict_id={dict_id}",
                    details={"dict_id": dict_id, "name": name, "lang": lang, "is_canonical": bool(is_canonical)},
                    trigger_source="add_aliases",
                )


def lookup_external_id(
    entity_type: str,
    external_key: str,
    external_value: str,
    session=None,
) -> dict | None:
    """
    在 entity_dict.external_ids JSONB 中查找匹配的外部 ID。
    external_ids 的结构如 {"stock_code": "000001", "cas_number": "50-78-2"}。
    命中返回 {"dict_id": int, "canonical_name": str}，未命中返回 None。
    session 可选：传入则复用，不传则自开（向后兼容）。
    """
    def _query(sess):
        row = sess.query(
            EntityDict.id,
            EntityDict.canonical_data,
        ).filter(
            EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == false(),
            text("entity_dict.external_ids ->> :key = :val"),
        ).params(key=external_key, val=str(external_value).strip()).first()
        if row:
            canonical = row.canonical_data or {}
            return {"dict_id": row.id, "canonical_name": canonical.get("name", "")}
        return None

    if session is not None:
        return _query(session)

    from graph.db.session import get_session
    with get_session() as sess:
        if sess is None:
            return None
        return _query(sess)


def get_entity_with_aliases(dict_id: int) -> dict | None:
    """获取 entity_dict 记录及所有 alias 名称，供 LLM 精排使用。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        entity = session.query(EntityDict).filter(
            EntityDict.id == dict_id, EntityDict.is_deleted == false()
        ).first()
        if not entity:
            return None
        aliases = session.query(EntityAlias.name).filter(
            EntityAlias.dict_id == dict_id,
            EntityAlias.is_deleted == false(),
        ).all()
        return {
            "dict_id": entity.id,
            "entity_type": entity.entity_type,
            "canonical_data": entity.canonical_data or {},
            "aliases": [a.name for a in aliases],
        }


def add_alias_with_embedding(
    dict_id: int,
    name: str,
    entity_type: str = "",
    lang: str = "",
    is_canonical: bool = False,
    embedding: list[float] | None = None,
) -> None:
    """
    添加别名并附带 embedding 向量。
    别名已存在则跳过。
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        exists = session.query(EntityAlias).filter(
            EntityAlias.dict_id == dict_id, EntityAlias.name == name,
            EntityAlias.is_deleted == false(),
        ).first()
        if exists is not None:
            return

        alias = EntityAlias(
            dict_id=dict_id,
            name=name,
            lang=lang or "",
            is_canonical=bool(is_canonical),
            entity_type=entity_type,
            name_embedding=embedding,
        )
        session.add(alias)
        session.flush()

        _write_operation_log(
            session, "insert", "entity_aliases", alias.id,
            business_action="alias_sync",
            description=f"添加别名(含embedding): '{name}' dict_id={dict_id}",
            details={"dict_id": dict_id, "name": name, "lang": lang, "is_canonical": bool(is_canonical),
                      "has_embedding": bool(embedding)},
            trigger_source="add_alias_with_embedding",
        )


def _write_aliases_with_embedding(
    session,
    dict_id: int,
    entity_type: str,
    entity_name: str,
    entity_data: dict,
    source_doc_uuid: str = "",
    allow_embedding_generation: bool = True,
) -> int:
    """将 entity_name + entity_data 中的名称写为 entity_aliases，附带 embedding。返回新增条数。"""
    from graph.extraction.llm_client import generate_embedding

    names_to_add: set[str] = set()
    names_to_add.add(entity_name.strip())

    data_name = entity_data.get("name", "").strip()
    if data_name:
        names_to_add.add(data_name)
    data_name_en = entity_data.get("name_en", "").strip()
    if data_name_en:
        names_to_add.add(data_name_en)
    for alias in (entity_data.get("aliases") or []):
        alias_str = str(alias).strip()
        if alias_str:
            names_to_add.add(alias_str)

    # 审核持锁路径只复用缓存；显式允许的兼容调用才生成缺失向量。
    embeddings_cache: dict[str, list[float]] = entity_data.pop("_embeddings", {}) or {}

    logger.info(f"[DB] alias write-back: dict_id={dict_id} names={list(names_to_add)}")

    added = 0
    for name in names_to_add:
        if not name:
            continue
        # 制剂后缀实体：不写入 entity_name 的子串作为 alias（如 "氯雷他定片" ← "氯雷他定"）
        if (_FORMULATION_SUFFIX.search(entity_name)
                and name != entity_name and name in entity_name):
            logger.info(f"[DB] alias '{name}' 是 entity_name '{entity_name}' 的子串，跳过")
            continue
        exists = (
            session.query(EntityAlias)
            .filter(EntityAlias.dict_id == dict_id, EntityAlias.name == name,
                    EntityAlias.is_deleted == false())
            .first()
        )
        if exists is not None:
            logger.debug(f"[DB] alias 已存在: '{name}' dict_id={dict_id}，跳过")
            continue

        # 跨 dict_id 冲突检查：该 name 已属于其他实体则跳过
        conflict = session.query(EntityAlias).filter(
            EntityAlias.dict_id != dict_id,
            EntityAlias.name == name,
            EntityAlias.entity_type == entity_type,
            EntityAlias.is_deleted == false(),
        ).first()
        if conflict is not None:
            logger.info(
                f"[DB] alias '{name}' 已属于 dict_id={conflict.dict_id}"
                f" (entity_type={entity_type})，跳过不写入 dict_id={dict_id}"
            )
            continue

        try:
            embedding = embeddings_cache.get(name)
            if not embedding and allow_embedding_generation:
                embedding = generate_embedding(name)
        except Exception as e:
            logger.warning(f"[DB] alias '{name}' embedding 生成失败: {e}")
            embedding = None

        alias = EntityAlias(
            dict_id=dict_id,
            name=name,
            lang="zh" if any('一' <= c <= '鿟' for c in name) else "en",
            is_canonical=(name == entity_data.get("name", "")),
            entity_type=entity_type,
            name_embedding=embedding,
        )
        session.add(alias)
        session.flush()

        if embedding:
            logger.debug(f"[DB] alias '{name}' written with embedding dims={len(embedding)}")
        else:
            logger.warning(f"[DB] alias '{name}' embedding 生成失败")
        added += 1

    if added and source_doc_uuid:
        names_written = [n for n in names_to_add if n]  # 所有写入的候选名
        _write_operation_log(
            session, "insert", "entity_aliases", None,
            business_action="alias_sync",
            description=f"LLM抽取别名: entity='{entity_name}' type={entity_type} dict_id={dict_id} aliases={names_written}",
            details={
                "source_doc_uuid": source_doc_uuid,
                "entity_name": entity_name,
                "entity_type": entity_type,
                "dict_id": dict_id,
                "aliases_written": names_written,
            },
            trigger_source="_write_aliases_with_embedding",
        )
    return added


def lookup_external_id_candidates(entity_type, external_key, external_value, session=None):
    from graph.db.identity import use_session
    with use_session(session) as active:
        rows = active.query(EntityDict).filter(EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == false(),
            EntityDict.external_ids[external_key].astext == str(external_value).strip()).order_by(EntityDict.id).all()
        return [{'dict_id': row.id, 'canonical_name': (row.canonical_data or {}).get('name', ''),
                 'entity_type': row.entity_type, 'canonical_data': row.canonical_data or {},
                 'external_ids': row.external_ids or {}} for row in rows]

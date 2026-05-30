"""
Neo4j 图数据库客户端 — 按 GRAPH_SYNC_DESIGN.md 推荐方式实现。
MERGE 幂等推送，关系类型白名单校验，连接单例管理。
"""
from neo4j import GraphDatabase, Driver
from loguru import logger
from config.settings import APP

# ── 实体类型白名单（GRAPH_SYNC_DESIGN.md §11）──────────────────────────────
_VALID_ENTITY_TYPES = frozenset({
    "Company", "Drug", "Target", "Indication", "ClinicalTrial",
    "DealEvent", "DealItem", "ApprovalEvent", "Policy",
    "TCMFormula", "Compound", "Pathway", "DevelopmentProject",
    "TrialIndication",
})

# ── 关系类型白名单（GRAPH_SYNC_DESIGN.md §12）──────────────────────────────
_VALID_RELATION_TYPES = frozenset({
    "DEVELOPS", "PARTICIPATES_IN", "INVESTED_IN", "SUBSIDIARY_OF",
    "SPONSORS", "ISSUES", "APPROVES", "PARTICIPATES_IN_PROJECT",
    "TARGETS", "TREATS", "IN_TRIAL", "HAS_APPROVAL", "HAS_ITEM",
    "INVOLVES_DRUG", "EVALUATES", "FOR_INDICATION", "IN_PATHWAY",
    "ASSOCIATED_WITH", "HOMOLOG_OF", "SUBTYPE_OF", "AFFECTS",
    "DEVELOPED_BY", "CONTAINS",
})


def _resolve_entity_node_id(entity_extracted_id: int | None) -> str:
    """将 extracted_entities.id 解析为 Neo4j 节点 ID（entity:{dict_id}）。"""
    if entity_extracted_id is None:
        return "entity:unknown"
    from db.session import get_session
    from db.models import ExtractedEntity
    with get_session() as session:
        if session is None:
            return f"entity:{entity_extracted_id}"
        row = session.query(ExtractedEntity.dict_id).filter(
            ExtractedEntity.id == entity_extracted_id,
        ).first()
        if row and row.dict_id:
            return f"entity:{row.dict_id}"
        return f"entity:{entity_extracted_id}"


class Neo4jClient:
    """Neo4j 连接单例，提供实体/关系推送和重试。"""

    def __init__(self):
        self._driver: Driver | None = None

    def _get_driver(self) -> Driver:
        if self._driver is not None:
            return self._driver
        if not APP.NEO4J_URI:
            raise RuntimeError("NEO4J_URI 未配置，无法连接 Neo4j")
        self._driver = GraphDatabase.driver(
            APP.NEO4J_URI,
            auth=(APP.NEO4J_USER, APP.NEO4J_PASSWORD),
        )
        logger.info(f"[Neo4j] 连接已建立: {APP.NEO4J_URI}")
        return self._driver

    # ═══════════════════════════════════════════════════════════════════
    # 实体推送（§10.1）
    # ═══════════════════════════════════════════════════════════════════

    def push_entity(self, entity: dict) -> str:
        """
        推送单个实体到 Neo4j，MERGE 幂等。
        entity 结构来自 get_approved_for_neo4j():
          id, entity_type, entity_name, entity_data, dict_id
        返回 Neo4j 节点 ID。
        """
        driver = self._get_driver()
        entity_type = entity["entity_type"]
        safe_type = entity_type if entity_type in _VALID_ENTITY_TYPES else "Other"

        node_id = f"entity:{entity['dict_id'] or entity['id']}"

        with driver.session() as session:
            session.run(
                f"""
                MERGE (n:GraphEntity:{safe_type} {{id: $id}})
                SET n.type = $type,
                    n.name = $name,
                    n.properties = $properties,
                    n.source_entity_id = $source_entity_id
                """,
                id=node_id,
                type=safe_type,
                name=entity.get("entity_name") or "",
                properties=entity.get("entity_data") or {},
                source_entity_id=str(entity["id"]),
            )
        logger.info(f"[Neo4j] 实体推送成功: {node_id} ({safe_type})")
        return node_id

    # ═══════════════════════════════════════════════════════════════════
    # 关系推送（§10.2）
    # ═══════════════════════════════════════════════════════════════════

    def push_relation(self, relation: dict) -> tuple[str, str]:
        """
        推送单个关系到 Neo4j。
        relation 结构来自 _relation_to_dict():
          id, from_entity_id, to_entity_id, relation_type, relation_props
        返回 (relation_id, from_node_id->to_node_id)。
        """
        driver = self._get_driver()
        rel_type = relation["relation_type"]
        if rel_type not in _VALID_RELATION_TYPES:
            raise ValueError(f"非法关系类型: {rel_type}")

        from_id = _resolve_entity_node_id(relation.get("from_entity_id"))
        to_id = _resolve_entity_node_id(relation.get("to_entity_id"))
        rel_id = f"relation:{relation['id']}"

        with driver.session() as session:
            session.run(
                f"""
                MATCH (from:GraphEntity {{id: $from_id}})
                MATCH (to:GraphEntity {{id: $to_id}})
                MERGE (from)-[r:{rel_type} {{id: $id}}]->(to)
                SET r.properties = $properties,
                    r.source_relation_id = $source_relation_id
                """,
                from_id=from_id,
                to_id=to_id,
                id=rel_id,
                properties=relation.get("relation_props") or {},
                source_relation_id=str(relation["id"]),
            )
        logger.info(f"[Neo4j] 关系推送成功: {rel_id} ({rel_type})")
        return rel_id, f"{from_id}->{to_id}"

    # ═══════════════════════════════════════════════════════════════════
    # 重试
    # ═══════════════════════════════════════════════════════════════════

    def retry_push_entity(self, entity_id: int) -> None:
        """重试推送单个实体（读取最新数据后推送）。"""
        from db.session import get_session
        from db.models import ExtractedEntity
        from db.repository import mark_entity_pushed, mark_entity_failed

        with get_session() as session:
            if session is None:
                raise RuntimeError("数据库未连接")
            row = session.query(ExtractedEntity).filter(
                ExtractedEntity.id == entity_id,
                ExtractedEntity.is_deleted == False,
            ).first()
            if row is None:
                raise ValueError(f"实体不存在: {entity_id}")

            entity = {
                "id": row.id,
                "entity_type": row.entity_type,
                "entity_name": row.entity_name,
                "entity_data": row.entity_data,
                "dict_id": row.dict_id,
            }

        try:
            node_id = self.push_entity(entity)
            mark_entity_pushed(entity_id, node_id)
        except Exception as e:
            mark_entity_failed(entity_id, str(e))
            raise

    def retry_push_relation(self, relation_id: int) -> None:
        """重试推送单个关系（读取最新数据后推送）。"""
        from db.session import get_session
        from db.models import EntityRelation
        from db.repository import mark_relation_pushed, mark_relation_failed

        with get_session() as session:
            if session is None:
                raise RuntimeError("数据库未连接")
            row = session.query(EntityRelation).filter(
                EntityRelation.id == relation_id,
                EntityRelation.is_deleted == False,
            ).first()
            if row is None:
                raise ValueError(f"关系不存在: {relation_id}")

            relation = {
                "id": row.id,
                "from_entity_id": row.from_entity_id,
                "to_entity_id": row.to_entity_id,
                "relation_type": row.relation_type,
                "relation_props": row.relation_props,
            }

        try:
            rel_id, edge = self.push_relation(relation)
            mark_relation_pushed(relation_id, edge.split("->")[0], edge.split("->")[1])
        except Exception as e:
            mark_relation_failed(relation_id, str(e))
            raise

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None
            logger.info("[Neo4j] 连接已关闭")


# ── 全局单例 ───────────────────────────────────────────────────────────────
_client: Neo4jClient | None = None


def get_neo4j_client() -> Neo4jClient:
    global _client
    if _client is None:
        _client = Neo4jClient()
    return _client

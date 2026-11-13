"""
WeKnora 图同步 HTTP 客户端。
调用 WeKnora 的 4 个图同步 API 端点，将 ZK 侧的实体字典、抽取实体、抽取关系
推送到 WeKnora，再由 WeKnora 投影到 Neo4j。

API 端点（参考 task_doc/graph-sync.md）：
  1. POST /entity-dict/batch-upsert            → 同步 entity_dict 镜像表
  2. POST /entity-dict/{kbId}/init-copies      → 初始化 PrimeKG 副本到 graph_entities
  3. POST /kb/:id/graph/entities/batch-upsert  → 批量 upsert 抽取实体
  4. POST /kb/:id/graph/relations/batch-upsert → 批量 upsert 抽取关系
"""
import httpx
from loguru import logger

from settings import APP


class WeKnoraGraphClient:
    """WeKnora 图同步 HTTP 客户端。"""

    def __init__(self):
        self._base_url: str = APP.WEKNORA_BASE_URL.rstrip("/")
        self._api_key: str = APP.WEKNORA_API_KEY
        self._kb_id: str = APP.WEKNORA_KB_ID

    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
        }

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self._base_url}/api/v1{path}"
        logger.info(f"[WeKnoraGraph] POST {url}")
        try:
            resp = httpx.post(url, json=body, headers=self._headers, timeout=60.0)
            logger.info(f"[WeKnoraGraph] 响应 {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"[WeKnoraGraph] HTTP {e.response.status_code}: {e.response.text[:500]}")
            raise
        except Exception as e:
            logger.error(f"[WeKnoraGraph] 请求失败: {url} | {e}")
            raise

    # ── 1. 同步 entity_dict 镜像表 ──────────────────────────────────────

    def batch_upsert_entity_dict(self, rows: list[dict]) -> dict:
        """
        将 entity_dict 行推送到 WeKnora 镜像表。
        参数 rows 需包含: id, entity_type, external_ids, canonical_data,
                        canonical_source, is_deleted
        返回: {"success": true, "data": {"upserted": N}}
        """
        if not rows:
            return {"success": True, "data": {"upserted": 0}}
        payload = {"rows": rows}
        logger.info(f"[WeKnoraGraph] 推送 entity_dict: {len(rows)} 行")
        logger.debug(f"[WeKnoraGraph] 首行样本: {rows[0] if rows else 'N/A'}")
        result = self._post("/entity-dict/batch-upsert", payload)
        logger.info(f"[WeKnoraGraph] entity_dict 同步完成: {result}")
        return result

    # ── 2. 初始化 PrimeKG 副本 ──────────────────────────────────────────

    def init_copies(self, kb_id: str = "") -> dict:
        """
        从 entity_dict 镜像表初始化 PrimeKG 副本到 graph_entities。
        返回: {"success": true, "data": {"upserted": N}}
        """
        kb = kb_id or self._kb_id
        url = f"{self._base_url}/api/v1/entity-dict/{kb}/init-copies"
        try:
            resp = httpx.post(url, headers=self._headers, timeout=120.0)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"[WeKnoraGraph] init_copies 完成: {result}")
            return result
        except httpx.HTTPStatusError as e:
            logger.error(f"[WeKnoraGraph] init_copies HTTP {e.response.status_code}: {e.response.text[:500]}")
            raise
        except Exception as e:
            logger.error(f"[WeKnoraGraph] init_copies 请求失败: {e}")
            raise

    # ── 3. 批量 upsert 抽取实体 ────────────────────────────────────────

    def batch_upsert_entities(self, entities: list[dict], kb_id: str = "") -> dict:
        """
        将抽取实体推送到 WeKnora graph_entities。
        参数 entities 每条需包含: id, entity_type, entity_name, entity_data,
                                source_site, confidence_score, review_status, is_deleted
        返回: {"success": true, "data": {"upserted": N, "deleted": N, "skipped": N}}
        """
        if not entities:
            return {"success": True, "data": {"upserted": 0, "deleted": 0, "skipped": 0}}

        kb = kb_id or self._kb_id
        payload = {
            "entities": [
                {
                    "source_entity_id": f"dict:{e.get('dict_id') or e['id']}",
                    "entity_type": e["entity_type"],
                    "entity_name": e["entity_name"],
                    "entity_data": e.get("entity_data", {}),
                    "source_site": e.get("source_site", ""),
                    "confidence_score": e.get("confidence_score", 0.0),
                    "review_status": e.get("review_status", "pending"),
                    "is_deleted": e.get("is_deleted", False),
                }
                for e in entities
            ]
        }
        logger.info(f"[WeKnoraGraph] 推送实体: {len(entities)} 条 → KB {kb}")
        result = self._post(
            f"/knowledge-bases/{kb}/graph/entities/batch-upsert", payload
        )
        logger.info(f"[WeKnoraGraph] 实体同步完成: {result}")
        return result

    # ── 4. 批量 upsert 抽取关系 ────────────────────────────────────────

    def batch_upsert_relations(self, relations: list[dict], kb_id: str = "") -> dict:
        """
        将抽取关系推送到 WeKnora graph_relations。
        参数 relations 每条需包含: id, from_entity_id, to_entity_id,
                                  relation_type, relation_props, is_deleted
        返回: {"success": true, "data": {"upserted": N, "deleted": N, "skipped": N}}
        """
        if not relations:
            return {"success": True, "data": {"upserted": 0, "deleted": 0, "skipped": 0}}

        kb = kb_id or self._kb_id
        payload = {
            "relations": [
                {
                    "source_relation_id": f"rel-{r['id']}",
                    "from_entity_id": f"dict:{r.get('from_dict_id') or r.get('from_entity_id')}",
                    "to_entity_id": f"dict:{r.get('to_dict_id') or r.get('to_entity_id')}",
                    "relation_type": r["relation_type"],
                    "relation_props": r.get("relation_props", {}),
                    "is_deleted": r.get("is_deleted", False),
                }
                for r in relations
            ]
        }
        logger.info(f"[WeKnoraGraph] 推送关系: {len(relations)} 条 → KB {kb}")
        result = self._post(
            f"/knowledge-bases/{kb}/graph/relations/batch-upsert", payload
        )
        logger.info(f"[WeKnoraGraph] 关系同步完成: {result}")
        return result


    # ── 5. 知识库校验 ─────────────────────────────────────────────────

    # ── 6. 文档同步（全文上传） ──────────────────────────────────────────

# 全局单例
_client: WeKnoraGraphClient | None = None


def get_weknora_graph_client() -> WeKnoraGraphClient:
    global _client
    if _client is None:
        _client = WeKnoraGraphClient()
    return _client

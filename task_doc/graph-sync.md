# 知识图谱数据同步 API

本文档面向 **ZK_Data-Fabric**，说明如何将实体与关系数据同步到 WeKnora，再由投影任务写入 Neo4j。

## 架构

```
ZK_Data-Fabric                      WeKnora                            Neo4j
──────────────                       ────────                           ─────

1. entity_dict
     POST /entity-dict/batch-upsert → entity_dict (镜像表)
     POST /entity-dict/{kbId}/init-copies  → graph_entities (primekg 副本)
                                        ↓ go run cmd/project --entities-only
                                        节点 + REFERENCES 边 ──────────→ ✓

2. extracted_entities
     POST entities/batch-upsert     → graph_entities

3. entity_relations
     POST relations/batch-upsert    → graph_relations
                                        ↓ go run cmd/project
                                       实体 + 关系 ──────────────────→ ✓
```

核心原则：
- **数据库是事实源，Neo4j 是投影。** 所有写入只到数据库。
- ZK 自行维护变更状态，变更时重新推送即可（upsert 幂等）。

---

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/entity-dict/batch-upsert` | 同步 entity_dict 镜像表 |
| POST | `/entity-dict/{kbId}/init-copies` | 从镜像表初始化 PrimeKG 副本到 graph_entities |
| POST | `/knowledge-bases/:id/graph/entities/batch-upsert` | 批量 upsert 抽取实体 |
| POST | `/knowledge-bases/:id/graph/relations/batch-upsert` | 批量 upsert 抽取关系 |

> **权限**：需要知识库创建者本人或 Admin+ 角色。

---

## 1. 同步 entity_dict 镜像表

```
POST /api/v1/entity-dict/batch-upsert
```

将 ZK entity_dict 的行推送到 WeKnora 镜像表。以 `id` 为主键，已存在则更新。

### 请求体

```json
{
  "rows": [
    {
      "id": 42,
      "entity_type": "gene/protein",
      "external_ids": {"ncbi_gene": "5261"},
      "canonical_data": {"name": "PHKG2"},
      "canonical_source": "primekg",
      "is_deleted": false
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `id` | int | **是** | ZK entity_dict.id |
| `entity_type` | string | **是** | PrimeKG 类型：`drug` / `disease` / `gene/protein` / `pathway` |
| `external_ids` | object | **是** | 外部 ID 映射，用于后续匹配 PrimeKG 节点 |
| `canonical_data` | object | **是** | 实体属性，至少含 `name` |
| `canonical_source` | string | **是** | `"primekg"` |
| `is_deleted` | boolean | 否 | 软删除标记 |

### 响应

```json
{"success": true, "data": {"upserted": 100}}
```

---

## 2. 初始化 PrimeKG 副本

```
POST /api/v1/entity-dict/{kbId}/init-copies
```

从 entity_dict 镜像表读取 `canonical_source='primekg'` 的行，转换类型后写入 `graph_entities`。写入后 `sync_status=pending`，需手动触发投影。

### 类型映射

| entity_dict.entity_type | graph_entities.entity_type | Neo4j Label |
|--------------------------|---------------------------|--------------|
| `drug` | `Drug` | `` `drug` `` |
| `disease` | `Indication` | `` `disease` `` |
| `gene/protein` | `Target` | `` `gene/protein` `` |
| `pathway` | `Pathway` | `` `pathway` `` |

### external_ids 映射

`entity_data` 中会包含 `external_ids`，投影时用于匹配 PrimeKG 原始节点：

| external_ids key | Neo4j node_source |
|------------------|-------------------|
| `drugbank` | `DrugBank` |
| `mondo` | `MONDO` |
| `ncbi_gene` | `NCBI` |
| `reactome` | `Reactome` |

### 响应

```json
{"success": true, "data": {"upserted": 1500}}
```

---

## 3. 批量 upsert 抽取实体

```
POST /api/v1/knowledge-bases/:id/graph/entities/batch-upsert
```

将 ZK `extracted_entities` 写入指定知识库。以 `source_entity_id` 为业务主键，已存在则更新。

### 请求体

```json
{
  "entities": [
    {
      "source_entity_id": "ent-001",
      "entity_type": "Drug",
      "entity_name": "SYSA1801",
      "entity_data": {"name": "SYSA1801", "modality": "ADC"},
      "confidence_score": 0.92,
      "review_status": "approved"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `source_entity_id` | string | **是** | 业务主键，幂等键 |
| `entity_type` | string | **是** | 实体类型（见白名单） |
| `entity_name` | string | **是** | 展示名称 |
| `entity_data` | object | 否 | 实体属性 JSON |
| `source_site` | string | 否 | 来源站点，`"primekg"` 会在投影时创建 REFERENCES 边 |
| `confidence_score` | number | 否 | 置信度 0~1 |
| `review_status` | string | 否 | `pending` / `approved` / `rejected` |
| `is_deleted` | boolean | 否 | 软删除标记 |

### 响应

```json
{"success": true, "data": {"upserted": 5, "deleted": 1, "skipped": 0}}
```

---

## 4. 批量 upsert 抽取关系

```
POST /api/v1/knowledge-bases/:id/graph/relations/batch-upsert
```

写入前校验两端实体已存在，以及关系方向合法。

### 请求体

```json
{
  "relations": [
    {
      "source_relation_id": "rel-001",
      "from_entity_id": "ent-001",
      "to_entity_id": "ent-002",
      "relation_type": "TARGETS",
      "relation_props": {"mechanism": "抑制剂"}
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `source_relation_id` | string | **是** | 业务主键 |
| `from_entity_id` | string | **是** | 起点实体的 source_entity_id |
| `to_entity_id` | string | **是** | 终点实体的 source_entity_id |
| `relation_type` | string | **是** | 关系类型（见白名单） |
| `relation_props` | object | 否 | 关系属性 JSON |
| `is_deleted` | boolean | 否 | 软删除标记 |

> 关系写入前必须先写入对应实体，否则返回 400。

### 响应

```json
{"success": true, "data": {"upserted": 3, "deleted": 0, "skipped": 0}}
```

---

## 实体类型白名单

```
Company      Drug          Target         Indication
ClinicalTrial    DealEvent     DealItem       ApprovalEvent
Policy       TCMFormula    Compound       Pathway
DevelopmentProject   TrialIndication
```

## 关系类型白名单 & 方向约束

| 关系类型 | 起点类型 | 终点类型 |
|----------|----------|----------|
| `DEVELOPS` | `Company` | `Drug` |
| `DEVELOPS` | `DevelopmentProject` | `Drug` |
| `PARTICIPATES_IN` | `Company` | `DealEvent` |
| `INVESTED_IN` | `Company` | `Company` |
| `SUBSIDIARY_OF` | `Company` | `Company` |
| `SPONSORS` | `Company` | `ClinicalTrial` |
| `ISSUES` | `Company` | `Policy` |
| `APPROVES` | `Company` | `ApprovalEvent` |
| `PARTICIPATES_IN_PROJECT` | `Company` | `DevelopmentProject` |
| `TARGETS` | `Drug` / `Compound` | `Target` |
| `TREATS` | `Drug` / `TCMFormula` | `Indication` |
| `IN_TRIAL` | `Drug` | `ClinicalTrial` |
| `HAS_APPROVAL` | `Drug` | `ApprovalEvent` |
| `HAS_ITEM` | `DealEvent` | `DealItem` |
| `INVOLVES_DRUG` | `DealItem` | `Drug` |
| `EVALUATES` | `ClinicalTrial` | `TrialIndication` |
| `FOR_INDICATION` | `TrialIndication` | `Indication` |
| `IN_PATHWAY` | `Target` | `Pathway` |
| `ASSOCIATED_WITH` | `Target` | `Indication` |
| `HOMOLOG_OF` | `Target` | `Target` |
| `SUBTYPE_OF` | `Indication` | `Indication` |
| `AFFECTS` | `Policy` | `Company` / `Drug` / `Indication` / `Target` |
| `DEVELOPED_BY` | `TCMFormula` | `Company` |
| `CONTAINS` | `TCMFormula` | `Compound` |

---

## REFERENCES 边自动创建

投影时处理 `source_site=primekg` 的实体：

1. 从 `entity_data.external_ids` 解析 `DrugBank:DB00945` / `NCBI:5261` 等
2. MATCH Neo4j 中 PrimeKG 节点（`primekg_id` + `node_source`）
3. `MERGE (zk)-[:REFERENCES]->(p)`

ZK 无需关心，投影自动完成。

---

## 投影

WeKnora 侧手动触发：

```bash
# 仅投影实体（初始化 primekg 副本后）
go run cmd/project/main.go --kb-id=<UUID> --tenant-id=1 --entities-only

# 全量投影
go run cmd/project/main.go --kb-id=<UUID> --tenant-id=1
```

投影读取 `sync_status IN (pending, failed)` 的行，MERGE/DELETE 到 Neo4j，然后更新状态。

---

## 完整流程

```bash
# 1. ZK 推送 entity_dict 镜像
curl -X POST "http://weknora:8080/api/v1/entity-dict/batch-upsert" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"rows": [
    {"id":42,"entity_type":"gene/protein","external_ids":{"ncbi_gene":"5261"},"canonical_data":{"name":"PHKG2"},"canonical_source":"primekg"}
  ]}'

# 2. 初始化 PrimeKG 副本 → graph_entities
curl -X POST "http://weknora:8080/api/v1/entity-dict/KB001/init-copies" \
  -H "Authorization: Bearer $TOKEN"

# 3. 投影实体 → Neo4j（含 REFERENCES 边）
go run cmd/project/main.go --kb-id=KB001 --tenant-id=1 --entities-only

# 4. ZK 推送抽取实体
curl -X POST "http://weknora:8080/api/v1/knowledge-bases/KB001/graph/entities/batch-upsert" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"entities": [...]}'

# 5. ZK 推送抽取关系
curl -X POST "http://weknora:8080/api/v1/knowledge-bases/KB001/graph/relations/batch-upsert" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"relations": [...]}'

# 6. 全量投影
go run cmd/project/main.go --kb-id=KB001 --tenant-id=1
```

---

## 同步策略

1. **先实体后关系**：关系依赖实体端点存在
2. **增量推送**：变更时重新推送，upsert 幂等
3. **分批**：单次请求不超过 500 条
4. **ZK 维护状态**：ZK 侧自行判断哪些数据有变更需要重新推送

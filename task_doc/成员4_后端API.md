# 成员4 需求文档 — 后端 API

## 你负责什么

新建 `api/` 目录，基于 FastAPI 实现审核后台的所有接口。你是前端（成员5）和数据库（成员2）之间的桥梁。

---

## 背景：你的接口覆盖流程图哪些阶段

```
第三阶段（LLM抽取）← 触发入口
  └─ POST /api/extraction/trigger

第四阶段（审核消歧）← 核心操作
  └─ GET/POST /api/review/entities
  └─ GET/POST /api/review/relations

第五阶段（推图）← 触发入口
  └─ POST /api/neo4j/sync

并行下游（RAG）← 触发入口
  └─ POST /api/weknora/sync
```

---

## 目录结构

```
api/
  __init__.py
  main.py          # FastAPI app 入口
  routers/
    documents.py   # 文档查询
    extraction.py  # LLM 抽取触发
    review.py      # 审核操作
    neo4j.py       # Neo4j 推送
    weknora.py     # WeKnora 同步
  schemas/
    request.py     # 请求体 Pydantic 模型
    response.py    # 响应体 Pydantic 模型
```

---

## 接口定义

### 1. 文档模块（routers/documents.py）

#### GET /api/documents
文档列表，支持分页和过滤。

请求参数（Query）：
```
page: int = 1
page_size: int = 20
source_site: str = ""       # 按来源过滤，空则不过滤
doc_status: str = ""        # AUTO_PROCESSED / PENDING_REVIEW / REVIEWED
date_from: str = ""         # 发布日期起，格式 YYYY-MM-DD
date_to: str = ""           # 发布日期止
```

响应体：
```json
{
  "total": 1234,
  "page": 1,
  "page_size": 20,
  "items": [
    {
      "id": 1,
      "uuid": "xxx",
      "title": "关于批准XXX的通知",
      "source_site": "nmpa",
      "publish_date": "2026-01-15",
      "doc_status": "AUTO_PROCESSED",
      "confidence_score": 0.9,
      "extraction_status": "pending",
      "weknora_sync_status": "pending"
    }
  ]
}
```

注意：列表接口不返回 `raw_content`，避免传输大文本。

#### GET /api/documents/{uuid}
文档详情，包含全文和附件。

响应体：
```json
{
  "id": 1,
  "uuid": "xxx",
  "title": "...",
  "raw_content": "...",
  "source_site": "nmpa",
  "publish_date": "2026-01-15",
  "doc_status": "AUTO_PROCESSED",
  "confidence_score": 0.9,
  "parse_method": "css_extract",
  "doc_type": "regulatory_notice",
  "language": "zh",
  "extra": {"drug_name": "阿司匹林"},
  "attachments": [
    {"file_name": "说明书.pdf", "file_url": "https://..."}
  ]
}
```

---

### 2. LLM 抽取模块（routers/extraction.py）

#### POST /api/extraction/trigger
触发对指定文档的 LLM 抽取。

请求体：
```json
{
  "uuids": ["uuid1", "uuid2"],   // 指定文档，空则取所有 pending
  "limit": 10                    // uuids 为空时最多处理多少条
}
```

响应体：
```json
{
  "triggered": 10,
  "message": "已触发10篇文档的LLM抽取任务"
}
```

说明：这个接口是异步触发，不等待抽取完成就返回。实际抽取由成员3的 `extraction_flow` 执行。

#### GET /api/extraction/status/{uuid}
查询单篇文档的抽取状态。

响应体：
```json
{
  "uuid": "xxx",
  "extraction_status": "done",
  "extraction_model": "qwen-plus",
  "extracted_at": "2026-05-14T10:30:00Z",
  "extraction_error": ""
}
```

#### GET /api/extraction/stats
查询所有文档的抽取状态统计，前端用于显示整体进度和轮询刷新。

响应体：
```json
{
  "pending": 234,
  "processing": 12,
  "done": 4890,
  "failed": 23
}
```

---

### 3. 审核模块（routers/review.py）

#### GET /api/review/entities
待审核实体列表。

请求参数（Query）：
```
page: int = 1
page_size: int = 20
entity_type: str = ""       # 按实体类型过滤
source_site: str = ""
```

响应体：
```json
{
  "total": 56,
  "items": [
    {
      "id": 1,
      "source_doc_uuid": "xxx",
      "entity_type": "Drug",
      "entity_name": "司美格鲁肽",
      "entity_data": {"name_cn": "司美格鲁肽", "phase_cn": "已上市"},
      "confidence_score": 0.75,
      "confidence_reason": "向量匹配，相似度0.75",
      "source_text": "司美格鲁肽注射液获批...",
      "review_status": "pending"
    }
  ]
}
```

#### GET /api/review/entities/{id}
单个实体详情。

#### POST /api/review/entities/{id}/approve
审核通过。

请求体：
```json
{
  "reviewer": "张三",
  "note": "确认是司美格鲁肽"
}
```

响应体：
```json
{"success": true}
```

#### POST /api/review/entities/{id}/reject
审核拒绝。请求体同上。

#### GET /api/review/relations
待审核关系列表。

响应体：
```json
{
  "total": 23,
  "items": [
    {
      "id": 1,
      "source_doc_uuid": "xxx",
      "relation_type": "DEVELOPS",
      "from_entity_id": 10,
      "to_entity_id": 20,
      "relation_props": {},
      "confidence_score": 0.85,
      "source_text": "恒瑞医药开发了SHR-1701...",
      "review_status": "pending"
    }
  ]
}
```

#### POST /api/review/relations/{id}/approve
#### POST /api/review/relations/{id}/reject

---

### 4. Neo4j 推送模块（routers/neo4j.py）

> 实际推送逻辑由成员1的 `flows/neo4j_push_flow.py` 实现，这里的 API 只是调用入口。

#### POST /api/neo4j/sync/entities
触发将审核通过的实体批量推送到 Neo4j。

请求体：
```json
{
  "limit": 100
}
```

响应体：
```json
{
  "pushed": 95,
  "failed": 5,
  "message": "实体推送完成"
}
```

#### POST /api/neo4j/sync/relations
触发将审核通过的关系批量推送到 Neo4j。

请求体：
```json
{
  "limit": 100
}
```

响应体：
```json
{
  "pushed": 80,
  "failed": 2,
  "message": "关系推送完成"
}
```

#### POST /api/neo4j/retry
重试单条推送失败的实体或关系。

请求体：
```json
{
  "object_type": "entity",   // "entity" 或 "relation"
  "object_id": 42
}
```

响应体：
```json
{
  "success": true,
  "error": ""
}
```

#### GET /api/neo4j/stats
推送状态统计。

响应体：
```json
{
  "entities": {"pending": 120, "pushed": 450, "failed": 3},
  "relations": {"pending": 80, "pushed": 300, "failed": 1}
}
```

#### GET /api/neo4j/failed
查询推送失败的记录，方便排查。

请求参数（Query）：
```
object_type: str = ""    // "entity" 或 "relation"，空则两者都返回
limit: int = 50
```

响应体：
```json
{
  "entities": [
    {
      "id": 1,
      "entity_name": "恒瑞医药",
      "entity_type": "Company",
      "neo4j_error": "连接超时",
      "neo4j_pushed_at": "2026-05-14T10:30:00Z"
    }
  ],
  "relations": [
    {
      "id": 5,
      "relation_type": "DEVELOPS",
      "from_entity_id": 10,
      "to_entity_id": 20,
      "neo4j_error": "节点不存在",
      "neo4j_pushed_at": "2026-05-14T10:31:00Z"
    }
  ]
}
```

---

### 5. WeKnora 同步模块（routers/weknora.py）

#### GET /api/weknora/pending
待同步文档列表。

响应体：
```json
{
  "total": 234,
  "items": [
    {
      "uuid": "xxx",
      "title": "...",
      "source_site": "nmpa",
      "publish_date": "2026-01-15"
    }
  ]
}
```

#### POST /api/weknora/sync/{uuid}
手动触发单篇文档同步到 WeKnora。

响应体：
```json
{"success": true, "message": "同步成功"}
```

#### POST /api/weknora/sync-batch
批量触发同步。

请求体：
```json
{"limit": 100}
```

响应体：
```json
{"triggered": 100}
```

#### GET /api/weknora/stats
同步状态统计。

响应体：
```json
{
  "pending": 234,
  "synced": 4890,
  "failed": 12
}
```

---

## 技术要求

1. 使用 FastAPI，所有接口有 Pydantic 请求/响应模型
2. 统一错误响应格式：`{"error": "错误描述", "code": 400}`
3. 所有接口加 CORS 支持（前端本地开发需要）
4. 异步触发类接口（抽取、推送）不等待任务完成，立即返回
5. 分页接口统一用 `page` + `page_size`，响应里带 `total`

---

## 与其他成员的依赖

- 依赖成员2的 `db/repository.py` 所有函数
- 依赖成员3的 `flows/extraction_flow.py`（触发抽取时调用）
- 依赖成员5的前端（提供接口文档，Day 1 冻结 endpoint 路径和响应结构）

---

## 交付标准

- `api/` 目录结构完整，所有接口实现
- 用 Swagger UI（FastAPI 自带）能看到所有接口文档
- 每个接口有基本的错误处理（404、422、500）
- 与成员5联调通过

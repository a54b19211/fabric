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

并行下游（RAG）← 自动进行，只提供查看与重试
  └─ GET /api/history/index/status、POST /api/history/index/{id}/retry
```

> 2026-11 更新：正文进 WeKnora 只有一条路径 `db/index_publication.py` + 桥接服务（发表循环里的 `index_tick` 自动发送）。
> 旧的 WeKnora 文档同步接口（待同步列表、单篇/批量同步、统计）已删除，不要再实现；见下文第 5 节。

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
    weknora.py     # WeKnora 图数据推送（只推图；正文索引在 history.py 的 /api/history/index）
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

### 5. 正文索引发布模块（routers/history.py，`/api/history/index`）

正文进 WeKnora 只有这一条路径：`db/index_publication.py` 建 `index_publications` 作业，发表循环里的 `index_tick`
自动发送并核对桥接服务回执。这里**没有**“手动同步一篇/一批”的接口，也不要另写一套正文同步；接口只负责查看和恢复受阻作业。
三个接口都要 `Authorization: Bearer <DATAFABRIC_HISTORY_ADMIN_TOKEN>`：缺凭据 401 `authentication_required`，不是管理员凭据 403 `admin_required`。
错误响应是 FastAPI 的 `{"detail": {"code": "...", "message": "..."}}`。

#### GET /api/history/index/counts
各状态作业数，没出现的状态记 0。

```json
{"states": {"queued": 3, "sending": 0, "indexing": 1, "ready": 4890, "waiting": 2, "blocked": 1, "disabling": 0, "disabled": 7}, "total": 4904}
```

#### GET /api/history/index/status
作业列表，按更新时间倒序。查询参数：`limit`（1–100，默认 100）、`state`（上面八种之一）、`source_id`（来源编号，取自
`GET /api/history/collection/catalog`）、`cursor`（上一页返回的 `next_cursor`）。参数不对返回 422。

```json
{
  "items": [
    {
      "id": "<64位作业编号>", "state": "blocked", "desired_state": "active",
      "source_record_id": "NCT00000001", "version_id": "<64位>", "artifact_key": "",
      "remote_id": "<64位>", "knowledge_id": null, "title": "...",
      "error": {"code": "index_unavailable", "message": "索引处理暂不可用；已发送请求保留对账状态"},
      "attempts": 6, "first_send_at": null, "next_check_at": "...", "updated_at": "...",
      "source": {"source_id": "clinicaltrials", "is_demo": false, "knowledge_base_id": "...", "source_scope": "...", "source_instance": "..."},
      "read_permitted": true, "receipt": {}
    }
  ],
  "next_cursor": "2026-11-04T08:00:00.123456+00:00|<64位作业编号>"
}
```

#### POST /api/history/index/{id}/retry
只对 `blocked` 作业生效：状态改回 `waiting`、尝试次数清零、立即到期，下一轮 `index_tick` 处理；处理时仍先向桥接服务核对已发请求，不会重复上传。

```json
{"id": "...", "scheduled": true, "state": "waiting"}
{"id": "...", "scheduled": false, "state": "ready", "reason": "not_blocked"}
{"id": "...", "scheduled": false, "state": "blocked", "reason": "leased"}
```

编号不存在 404 `index_publication_not_found`，格式不是 64 位小写十六进制 422 `index_publication_id`。

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

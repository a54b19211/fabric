# ZK_Data Fabric 开发任务清单

> 更新时间：2026-05-13
> 爬取监控 UI 由 **Prefect 自带 Dashboard** 覆盖，无需自行开发。
> 需要自行开发的部分：数据库补全、数据操作层、审核后台 API + 前端、WeKnora 对接接口。

---

## 业务流程总览

```
步骤1  爬虫入库
       每条页面/政策 → documents 表，extraction_status = pending

步骤2  用户查看待处理数据
       按来源站点筛选，看到所有未过 LLM 的文档

步骤3  用户触发 LLM 抽取
       选中文档 → 点"过 LLM" → 后台异步处理：
         ① 组装 prompt（原文 + 来源预设模板 + Neo4j schema 约束）
         ② 调 LLM → 解析 JSON → Pydantic 字段校验
         ③ 实体消歧（你负责）
         ④ 结果存库，extraction_status = done

步骤4  用户审核
       图谱可视化展示实体和关系
       实体分三类：精确匹配（可直接通过）/ 向量匹配建议（需确认）/ 新实体（需确认）
       支持：一键通过高置信度内容、修改实体字段、重新过 LLM（可改 prompt）

步骤5  推送下游
       审核通过 → 推送到 Neo4j（实体 + 关系）
       文档全文 → 推送到 WeKnora（RAG）
```

---

## 派工表（你 + 5 成员）— 并行方案

**核心思路**：Day 1 上午全组花半天冻结接口契约，之后所有人基于契约并行开发，联调阶段再对接。

| 人员 | 负责模块 | 核心交付物 |
|------|----------|-----------|
| **你** | 消歧引擎 | `disambiguation/engine.py`：精确匹配 + 向量检索 + LLM 精排，对外暴露 `disambiguate()` 函数 |
| **成员 1** | 数据库 | `db/schema.sql` + `db/models.py` + 迁移文件（含 pgvector + `name_embedding` 字段） |
| **成员 2** | 数据操作层 | `db/repository.py` 全部函数 |
| **成员 3** | 数据模型 + LLM 抽取 flow | `biz_rules/schemas.py` + `flows/extraction_flow.py` + `prompts/` + 爬虫适配 |
| **成员 4** | 后端 API | `api/` 目录，FastAPI，含触发LLM、审核、消歧确认、WeKnora同步接口 |
| **成员 5** | 前端 + WeKnora 改造 | 审核工作台（图谱可视化）+ `sync_to_weknora.py` 改造 |

### 并行开发时序

```
Day 1 上午（全组，约半天）
  └─ 冻结契约：schema 字段 + repository 函数签名 + API endpoint + disambiguate() 返回结构

Day 1 下午起，全部并行：
  你       ──► disambiguation/engine.py（精确匹配 + 向量检索 + LLM 精排）
  成员 1   ──► schema.sql / models.py / 迁移文件 / pgvector
  成员 2   ──► repository.py（用 mock DB 先跑通逻辑）
  成员 3   ──► schemas.py / extraction_flow.py / prompts/ / 爬虫适配
  成员 4   ──► FastAPI（repository 和 disambiguate 用 mock）
  成员 5   ──► 前端审核工作台 + WeKnora 改造（API 用 mock server）

Day 4~5（联调）：
  成员 1 + 成员 2：mock DB → 真实 DB
  你 + 成员 2：消歧引擎接入真实 lookup_alias / search_similar_entities
  成员 3 + 你：extraction_flow 调用 disambiguate()
  成员 2 + 成员 4：mock repository → 真实实现
  成员 4 + 成员 5：mock API → 真实 API
```

### Day 1 需要冻结的契约

**你 → 所有人**：`disambiguate()` 函数签名和返回结构
```python
def disambiguate(name_cn, name_en, entity_type) -> DisambiguationResult
# status: auto_matched / vector_matched / new_entity
# dict_id, confidence, matched_name, reason
```

**成员 1 → 所有人**：所有表的字段名、类型、默认值

**你 → 成员 2**：消歧需要的两个查询函数签名
```python
lookup_alias(name, entity_type) -> int | None
search_similar_entities(embedding, entity_type, top_k) -> list[dict]
```

**成员 4 → 成员 5**：所有 API endpoint 的路径、请求体、响应体结构

---

---

## 模块一：数据库补全

### 1.1 db/schema.sql
- [ ] `attachments` 表新增 `local_path TEXT` 字段（区分远程 URL 和本地存储路径）
- [ ] `extracted_entities` 表新增 `source_text TEXT` 字段（LLM 抽取时的原文片段，供前端高亮）
- [ ] 新增 `crawl_log` 表（记录每次 Prefect flow run 的执行情况：站点、类型、状态、成功/失败数）
- [ ] 新增 `review_log` 表（记录每次审核操作：对象类型、操作、审核人、前后状态）
- [ ] 为以上新增字段和表补充对应索引

### 1.2 db/models.py（ORM 与 schema 同步）
- [ ] `Attachment` 模型新增 `local_path` 字段
- [ ] `ExtractedEntity` 模型新增 `source_text` 字段
- [ ] 新增 `CrawlLog` ORM 模型
- [ ] 新增 `ReviewLog` ORM 模型

### 1.3 migration/versions/V002__v3_schema.sql（增量迁移，用于已有数据库）
- [ ] `ALTER TABLE attachments ADD COLUMN local_path TEXT`
- [ ] `ALTER TABLE extracted_entities ADD COLUMN source_text TEXT`
- [ ] `CREATE TABLE crawl_log`
- [ ] `CREATE TABLE review_log`
- [ ] 补充新索引

---

## 模块二：数据操作层（db/repository.py）

> 当前文件是空 placeholder，需全部实现。

### 2.1 文档写入
- [ ] `upsert_document(envelope)` — 幂等写入（documents + doc_extra + attachments），同一 UUID 重跑不重复
- [ ] `upsert_doc_extra(uuid, source_site, extra)` — 供 db/sites/ 各写入器调用

### 2.2 状态更新
- [ ] `update_extraction_status(uuid, status, model, error)` — 更新 LLM 抽取状态
- [ ] `update_weknora_sync_status(uuid, status)` — 更新 WeKnora 同步状态

### 2.3 查询接口
- [ ] `get_pending_extraction(limit)` — 查询 extraction_status=pending 的文档
- [ ] `get_pending_weknora_sync(limit)` — 查询 weknora_sync_status=pending 的文档

### 2.4 实体消歧
- [ ] `disambiguate_entity(entity_name, entity_type)` — 查 entity_aliases 精确匹配，命中返回 dict_id，未命中返回 None

### 2.5 抽取结果写入
- [ ] `insert_extraction_result(result)` — 批量写入实体 + 关系，含消歧逻辑，自动设置 review_status

### 2.6 canonical 数据合并
- [ ] `merge_canonical_data(dict_id, new_data, source_site)` — 三种合并规则：
  - 数组字段（aliases/types）：追加去重，永不覆盖
  - 状态字段（phase_cn/global_status）：高优先级来源覆盖低优先级（nmpa/fda=10 > chictr=8 > hkex=6 > 新闻=3）
  - 快照字段（market_cap/price）：时间新的覆盖旧的

### 2.7 审核队列接口
- [ ] `get_pending_review_entities(limit)` — 查询待审核实体
- [ ] `approve_entity(id, reviewer, note)` — 审核通过，更新 review_status + 写 review_log
- [ ] `reject_entity(id, reviewer, note)` — 审核拒绝，更新 review_status + 写 review_log
- [ ] `get_pending_review_relations(limit)` — 查询待审核关系
- [ ] `approve_relation(id, reviewer, note)`
- [ ] `reject_relation(id, reviewer, note)`

### 2.8 Neo4j 推送状态接口
- [ ] `get_approved_for_neo4j(limit)` — 查询 review_status=approved 且 neo4j_status=pending 的实体
- [ ] `mark_entity_pushed(id, neo4j_node_id)` — 标记推送成功
- [ ] `mark_entity_failed(id, error)` — 标记推送失败

### 2.9 日志接口
- [ ] `log_crawl_task(site_key, task_type)` — 插入 crawl_log 记录，返回 log_id
- [ ] `update_crawl_log(log_id, status, total, passed, failed, error)` — flow 结束时更新
- [ ] `log_review_action(object_type, object_id, action, reviewer, note, before_status, after_status)` — 写 review_log

---

## 模块三：Pydantic 模型补全（biz_rules/schemas.py）

- [ ] `AttachmentInfo` 新增 `local_path: str` 字段
- [ ] `ExtractedEntityRecord` 新增 `source_text: str` 字段
- [ ] `ExtractionResult` 新增 `source_text: str` 字段
- [ ] 为 11 种实体类型各定义 Pydantic 模型：
  - `CompanyEntity`、`DrugEntity`、`TargetEntity`、`IndicationEntity`
  - `ClinicalTrialEntity`、`DealEventEntity`、`ApprovalEventEntity`
  - `PolicyEntity`、`PathwayEntity`、`TCMFormulaEntity`、`CompoundEntity`
- [ ] 新增 `ENTITY_SCHEMA_MAP: dict` — 映射 entity_type 字符串 → 对应 Pydantic 模型

---

## 模块四：爬虫层适配

### 4.1 core_engine/storage_writer.py
- [ ] 组装 envelope 时写入 `raw_content`（Markdown 全文直接入库，不只存文件路径）
- [ ] 组装附件时带上 `local_path`（本地下载路径）
- [ ] Flow 开始时调用 `log_crawl_task` 插入日志记录
- [ ] Flow 结束时调用 `update_crawl_log` 更新统计数据

### 4.2 db/sites/ 写入器
- [ ] `db/sites/chictr.py` — 确认与新版 `upsert_doc_extra` 接口签名一致
- [ ] `db/sites/hkex.py` — 同上
- [ ] 新建 `db/sites/pubmed.py` — 写入 pmid、doi、journal_name 到 doc_extra
- [ ] 新建 `db/sites/clinicaltrials.py` — 写入 nct_id 等字段到 doc_extra

---

## 模块五：审核后台 API（新建 api/ 目录，基于 FastAPI）

### 5.1 文档模块
- [ ] `GET /api/documents` — 文档列表（分页，支持按 source_site / doc_status / publish_date 过滤）
- [ ] `GET /api/documents/{uuid}` — 文档详情（含 raw_content 全文、附件列表、doc_extra）

### 5.2 审核模块
- [ ] `GET /api/review/entities` — 待审核实体列表（review_status=pending，含 source_text）
- [ ] `GET /api/review/entities/{id}` — 单个实体详情
- [ ] `POST /api/review/entities/{id}/approve` — 通过（body: reviewer, note）
- [ ] `POST /api/review/entities/{id}/reject` — 拒绝（body: reviewer, note）
- [ ] `GET /api/review/relations` — 待审核关系列表
- [ ] `POST /api/review/relations/{id}/approve`
- [ ] `POST /api/review/relations/{id}/reject`

### 5.3 WeKnora 对接模块
- [ ] `GET /api/weknora/pending` — 待同步文档列表（weknora_sync_status=pending）
- [ ] `POST /api/weknora/sync/{uuid}` — 手动触发单篇同步
- [ ] `POST /api/weknora/sync-batch` — 批量触发同步（body: limit）
- [ ] `GET /api/weknora/stats` — 同步状态统计（pending/synced/failed 各多少条）

---

## 模块六：审核前端（新建 frontend/ 目录）

> 爬取监控直接用 Prefect Dashboard，无需开发。
> 前端只需覆盖审核工作台和 WeKnora 同步管理。

### 6.1 文档列表页
- [ ] 表格展示：标题、来源站点、发布日期、置信度、doc_status
- [ ] 筛选：按 source_site、doc_status、日期范围
- [ ] 点击跳转到审核页

### 6.2 审核工作台页（核心）
- [ ] 左侧：原文全文（Markdown 渲染），高亮 `source_text` 对应片段
- [ ] 右侧：实体卡片列表（类型、名称、置信度、entity_data 字段展示）
- [ ] 右侧：关系卡片列表（起点、终点、关系类型、置信度）
- [ ] 操作：每条实体/关系独立通过 / 拒绝按钮
- [ ] 操作：批量全部通过 / 全部拒绝
- [ ] 审核备注输入框

### 6.3 WeKnora 同步管理页
- [ ] 展示待同步 / 已同步 / 失败的文档列表
- [ ] 失败条目显示错误原因
- [ ] 手动触发重试按钮

---

## 模块七：WeKnora 对接（sync_to_weknora.py 改造）

- [ ] 将现有脚本逻辑封装为可被 API 调用的函数（不只是命令行脚本）
- [ ] 同步成功后调用 `update_weknora_sync_status(uuid, 'synced')`
- [ ] 同步失败后记录错误原因，支持重试
- [ ] （可选）新建 `api/weknora_webhook.py` — 接收 WeKnora 回调确认

---

## 开发优先级建议

| 优先级 | 模块 | 原因 |
|--------|------|------|
| P0 | 模块一（数据库）+ 模块二（repository） | 其他所有模块的基础 |
| P1 | 模块三（Pydantic）+ 模块四（爬虫适配） | 让爬虫数据能正确落库 |
| P2 | 模块五（后端 API） | 审核前端的依赖 |
| P3 | 模块六（审核前端） | 依赖后端 API |
| P4 | 模块七（WeKnora 对接） | 接口字段已预留，可最后做 |

# ZK Data Fabric — 数据审核系统架构介绍

> 更新日期：2026-09-21
> 本文档基于当前代码库（分支 `test-suite`）实际实现整理，涵盖架构现状、已有能力与待完善项。

---

## 1. 项目概述

ZK Data Fabric（前端产品名 DataPulse）是一个**面向药学领域的知识图谱数据生产与人工审核系统**。

它的完整业务闭环是：

```
采集网页/PDF/Excel → 清洗质检 → 入库 →
LLM 结构化抽取（实体 + 关系）→ 实体消歧 →
人工审核（含 Agent 自动审核）→ 实体字典归并 →
同步到 WeKnora（全文 RAG）+ 推送 Neo4j 图投影
```

系统定位是「数据生产方」：负责抽取、清洗、审核、来源追溯、置信度评估；图查询、图展示由下游 Neo4j 承担（见 `GRAPH_SYNC_DESIGN.md` 的设计原则：**数据库是事实源，Neo4j 是图谱投影**）。

---

## 2. 核心业务流程（端到端）

```
① 采集入库
   网页/PDF/Excel/Markdown → documents 表，extraction_status = pending

② 触发 LLM 抽取
   选中/批量选中 pending 文档 → 入任务队列 → 后台异步处理：
     - 智能分块 → 按来源站点组装 prompt → 调 LLM → 解析 JSON → Pydantic 校验
     - 实体消歧（精确匹配 + 向量召回 + Reranker 精排）
     - 结果写入 extracted_entities / entity_relations，extraction_status = done

③ 审核
   实体分四类：精确匹配(auto_matched) / 候选匹配(candidate_matched) /
   新实体(new_entity) / 模糊(ambiguous)
   支持：单条通过/拒绝、批量通过高置信度、修改实体字段、
   重新抽取、Agent 自动审核、实体消歧（AI 链接/转人工）

④ 实体归并
   审核通过后写入实体字典 entity_dict（含别名、canonical 数据）
   多来源字段冲突进入 entity_merge_queue 人工/Agent 裁决
   重复实体走身份合并（entity_identity_merge_proposal + merge_log 回滚）

⑤ 推送下游
   文档全文 → WeKnora（RAG 知识库）
   审核通过的实体/关系 → WeKnora 的 Neo4j 拷贝库（图投影）
```

---

## 3. 技术栈

### 3.1 后端

| 类别 | 技术 | 说明 |
|---|---|---|
| 语言 | Python 3.11.9 | `.venv` 虚拟环境 |
| Web 框架 | FastAPI + uvicorn | `api/main.py`，端口 8000 |
| ORM | SQLAlchemy 2.x + psycopg2 | PostgreSQL |
| 向量检索 | pgvector | `text-embedding-v3`，1024 维，ivfflat 索引 |
| 采集调度 | 来源目录 + 刷新任务 | `collection/catalog.py`、`db/source_refresh.py` |
| 任务队列 | RQ + Redis（可插拔） | 默认 memory，可切 redis |
| Agent 编排 | LangGraph + LangChain | StateGraph（ReAct） |
| LLM | OpenAI 兼容协议 | Qwen/DashScope，抽取/审核/消歧共用 |
| 配置 | pydantic-settings | 读 `.env` |

### 3.2 前端

| 类别 | 技术 |
|---|---|
| 框架 | Vue 3 + TypeScript + Vite 5 |
| UI 组件库 | Element Plus（青绿 Teal 主题，品牌色 `#28aa86`） |
| 状态管理 | Pinia |
| 路由 | Vue Router 4 |
| HTTP | Axios（`baseURL: /api`） |
| 图可视化 | AntV G6（GraphReview/KnowledgeGraph）+ Cytoscape（单文档图谱） |
| Markdown | marked |

> 项目内还存在 `frontend-next/`（Next.js，作为设计体系参考源）、`WeKnora-main/`（WeKnora 的 docreader + mcp-server 代码）与 `Weknora-frontend/`（空目录），均非本项目主力代码，边界待厘清（见 §10）。

---

## 4. 系统分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                     前端 frontend/（Vue3）                    │
│  文档列表/审核工作台/图谱审核/实体消歧/合并/Neo4j/WeKnora/Agent │
└──────────────────────────┬──────────────────────────────────┘
                           │ Axios /api/*
┌──────────────────────────▼──────────────────────────────────┐
│                 后端 API（api/，FastAPI，89 个端点）           │
│  documents / extraction / review / weknora / agent 五个路由    │
└──────┬──────────────┬──────────────┬──────────────┬──────────┘
       │              │              │              │
┌──────▼──────┐ ┌─────▼─────┐ ┌──────▼──────┐ ┌─────▼─────────┐
│ 数据操作层   │ │ 抽取链路   │ │ Agent 审核   │ │ 图同步客户端    │
│ db/         │ │ flows/    │ │ core_engine/ │ │ core_engine/  │
│ repository/ │ │ biz_rules/│ │ agents/     │ │ weknora_graph │
│ models/     │ │ disambig/ │ │ agent_*     │ │ _client.py    │
└──────┬──────┘ └─────┬─────┘ └──────┬──────┘ └─────┬─────────┘
       │              │              │              │
┌──────▼──────────────▼──────────────▼──────────────▼─────────┐
│        基础设施：PostgreSQL(pgvector) / Redis(RQ) / LLM        │
└──────────────────────────────────────────────────────────────┘
```

模块职责：

| 目录 | 职责 |
|---|---|
| `api/` | FastAPI 入口 + 5 个 router + Pydantic schemas |
| `db/` | ORM 模型、session、repository（数据操作层）、审核生命周期/监听器 |
| `migration/` | 自研轻量迁移器 + V001~V015 SQL 迁移 |
| `flows/` | 爬虫 flow、抽取 flow、worker、分块 |
| `biz_rules/` | Pydantic 实体 schema、抽取 prompt、来源映射、各站点抽取器 |
| `disambiguation/` | 实体消歧引擎（规则 + 向量 + Reranker） |
| `core_engine/` | 爬虫/解析/质检/入库、任务队列、Agent 及其工具、图同步客户端 |
| `config/` | 全局配置、站点配置（`config/sites/` 递归加载） |
| `frontend/` | Vue3 前端 |

---

## 5. 数据模型

PostgreSQL，共 **15 张表**（`db/models.py` + 迁移），全局风格：**软删除 + 无外键**（关联完整性由 repository 事务逻辑保证）。

| 表 | 用途 | 关键字段 |
|---|---|---|
| `documents` | 原始文档 | `uuid`(去重键)、`raw_content`、`source_site`、`extraction_status`、`weknora_sync_status`、`doc_status`、`pending_entity_count`/`total_entity_count` |
| `doc_extra` | 来源特有字段 | `extra`(JSONB, GIN 索引) |
| `attachments` | 附件 | `local_path`、`file_url` |
| `entity_dict` | **实体字典（消歧主表）** | `entity_type`、`external_ids`(JSONB)、`canonical_data`(JSONB)、`is_verified` |
| `entity_aliases` | 实体别名查找 | `name`、`is_canonical`、`name_embedding`(vector(1024)) |
| `extracted_entities` | **LLM 抽取的图节点暂存** | `entity_type`、`entity_name`、`entity_data`(JSONB)、`dict_id`、`source_text`、`review_status`、`weknora_graph_status` |
| `entity_relations` | **LLM 抽取的图边暂存** | `from_entity_id`/`to_entity_id`、`relation_type`、`relation_props`、`review_status` |
| `crawl_log` | 爬取日志 | `site_key`、`task_type`、`status` |
| `review_log` | 审核操作日志 | `object_type`、`action`、`before_data`/`after_data` |
| `operation_log` | 写操作审计日志 | `business_action`、`details`、`trigger_source` |
| `entity_merge_queue` | 属性合并冲突队列 | `field_name`、`existing_value`/`new_value`、`resolution` |
| `entity_merge_log` | 实体合并日志（快照+回滚） | `keeper_dict_id`、`absorbed_dict_ids`(BIGINT[])、`before_snapshot`/`after_snapshot` |
| `entity_identity_merge_proposal` | 身份合并提案 | `source_entity_snapshot`/`target_dict_snapshot`、`agent_reason` |
| `agent_run` | Agent 运行记录 | `agent_type`、`decision`、`steps`(JSONB) |
| `schema_migrations` | 迁移版本登记 | `version`、`checksum` |

### 状态机速查

| 字段 | 取值 |
|---|---|
| `documents.extraction_status` | `pending` → `queued` → `processing` → `done` / `failed` |
| `extracted_entities.review_status` | `pending` → `auto_approved` / `approved` / `rejected` |
| `entity_relations.review_status` | `pending` → `approved` / `rejected` |
| `documents.weknora_sync_status` / `entity_dict.weknora_sync_status` | `pending` / `synced` / `failed` |
| `extracted_entities/relations.weknora_graph_status` | `pending` / `pushed` / `failed` |
| `documents.doc_status` | `AUTO_PROCESSED` / `PENDING_REVIEW` / `REVIEWED` |
| `entity_merge_queue.resolution` | `pending` → `accept_new` / `keep_existing` / `custom` / `resolved_*` |
| `entity_identity_merge_proposal.status` | `pending` / `approved` / `rejected` |

---

## 6. 核心链路详解

### 6.1 采集链路（`collection/` + `biz_rules/history_sources.py` 的读取插件）

```
来源目录 collection/catalog.py → 读取插件（public_api_reader / who_don / sec / site_crawler …）
  → 只追加 history 版本（原件 + 确定性投影，三类时间）
  → 附件各成版本 → processing.attachment_parse（MinerU）→ derived_clean_text
  → 清洗段 processing.clean_text / identifiers / profile → 派生表
  → db/index_publication → 桥接服务 → WeKnora
```

- 站点配置在 `config/sites/` 下（`SITE_KEY` + `CONFIG`），由 `collection/site_crawler.py` 统一读取：详情页、表格页、接口站点三种。
- 站点来源（catalog 里 plugin=site_crawler 的 18 个）都标 `verified=False`，未对原站实测。
- 重抓用 `query=by_url`；不写 documents 表，documents 库存只经 `legacy_documents` 冻结进 history。
- 抽取、审核、智能体、消歧目前仍读写 documents 库与实体表（只有这一套；迁到 history 版本是 v3 3.4/3.5）。

### 6.2 LLM 抽取链路（NER：命名实体识别 + 关系抽取，`flows/extraction_flow.py`）

```
split_document(智能分块)
  → get_extraction_prompt(按 source_site 选模板)
  → qwen_extract_structured(OpenAI 兼容, stream)
  → _parse_chunk_output(白名单校验 + Pydantic 校验)
  → _merge_chunks(跨块去重 + 关系下标重映射)
  → disambiguate_entities(消歧)
  → pg_advisory_xact_lock(全局写库串行锁)
  → insert_extraction_result(入库 + 交叉验证 + 状态更新)
```

- 任务队列只存 `uuid`，worker 执行时再查库取 `raw_content`（`core_engine/task_queue.py`）。
- 队列后端可插拔：`memory`（线程，默认）或 `redis`（RQ，队列名 `extraction`，job 超时 18000s）。
- 崩溃兜底：`reset_stale_extractions` + `recover_pending`。
- **实体 16 种、关系 25 种**，字段与 Neo4j 节点严格对齐（`biz_rules/entity_schemas.py`）。

### 6.3 实体消歧（NEL：命名实体链接，`disambiguation/engine.py`，六阶段）

```
Step1 强 ID 匹配(external_ids，如 CAS/stock_code/approval_number/trial_id) → auto_matched ← 落锤写 dict_id
Step2 别名精确匹配(entity_aliases) → auto_matched ← 落锤写 dict_id
Step3 类型规则匹配(仅 Company/Drug/Target) → auto/candidate_matched
Step4 向量召回(pgvector 余弦近邻, top_k=10) → 候选集
Step5 BGE Reranker 精排 → candidate_matched / ambiguous / new_entity
Step6 LLM 兜底精排 —— ⚠️ 当前被注释禁用（方案决策：降级为分级路由的「中置信度复核」，仅在 0.7–0.9 区间触发；≥0.9 由 Reranker 自动落锤、<0.7 交人工）
```

> 关键设计：**Step1 强 ID 匹配 + Step2 别名精确匹配会真正写入 `dict_id` 并合并**（2026-09-21 起 Step1 强 ID 恢复落锤，`engine.py:213` 条件 `match_step in ("step2_exact_alias", "step1_strong_id")`，确定性唯一 ID 直接落锤）；Step3 规则 / Step5 Reranker 结果只进入 `entity_data["_disambig"].candidates` 供前端展示，由人工在审核时确认链接（落锤自动化留给课题 4.3 分级置信度路由）。

### 6.4 人工审核（`api/routers/review.py` + 前端）

- 单文档审核工作台（`ReviewWorkbench`）：左原文（高亮 `source_text`）/ 右实体、关系、合并三个标签页。
- 批量审核工作台（`BatchReviewWorkbench`）：跨文档分页批量审核。
- 图谱审核（`GraphReview`）：全量知识图谱 G6 可视化，支持搜索、过滤、邻接展开、多选/框选批量审核。
- 审核通过实体走 `approve_entity_with_alias_writeback`（合并 canonical_data + 别名回写 + embedding + 去重）。
- 交叉验证：同一 `dict_id` 第二次命中自动置 `auto_approved`。

### 6.5 实体归并与冲突裁决

- **属性冲突**：多来源字段冲突进 `entity_merge_queue`，人工（`FieldConflict`）或 Agent 裁决（keep_existing / accept_new / custom）。
- **身份合并**：重复 `entity_dict` 通过 `MergeWorkbench` 手动合并，或由 Agent 生成 `entity_identity_merge_proposal` 提案人工确认；合并全程写 `entity_merge_log`（含前后快照），支持 `rollback_merge` 回滚。

### 6.6 WeKnora 同步与 Neo4j 图投影

- 文档全文 → WeKnora 知识库（RAG），`documents.weknora_sync_status` 追踪。
- 审核通过的实体/关系 → WeKnora 的 Neo4j 拷贝库（`core_engine/weknora_graph_client.py`），`weknora_graph_status` 追踪。
- 分三步：`entity_dict` 镜像 → `graph_entities` → `graph_relations`，可单步或全量同步。

---

## 7. Agent 自动审核（`core_engine/agent_runner.py` + `agents/`）

统一入口 `AgentRunner`（单例 `get_agent_runner()`），五种 Agent：

| Agent | 模式 | 说明 |
|---|---|---|
| A 实体审核 | Pipeline（3 步） | 有效性 → 类型正确性 → 原文依据，各一次 LLM 调用 |
| A' 关系审核 | Pipeline（3 步） | 端点存在性(本地) → 类型合法性 → 原文依据 |
| B 冲突裁决 | ReAct（StateGraph） | 属性冲突 keep_existing / accept_new / custom |
| C 语义匹配 | ReAct（StateGraph） | 判断两 dict 是否同实体 |
| D 实体合并消歧 | ReAct（StateGraph） | extracted_entity 链接到 entity_dict，SSE 流式推理 |

- 工具集：`db_tools`(检索/详情)、`web_tools`(联网搜索)、`rule_tools`(类型规则)、`decision_tools`(提交决策)。
- 决策持久化到 `agent_run` 表，前端可查看推理步骤（`ReasoningTimeline`）。
- 来源权威度配置（`agent_config.py`）：`nmpa:10 > cde/fda:9 > drugbank:8 > pubmed:7 > ... > hkex/roche:3`。

### 7.1 Agent 在工作流中的位置

Agent 自动审核位于工作流**第③步「审核」环节**——夹在「LLM 抽取(②)」与「实体归并(④)」之间，消费 `review_status=pending` 数据，与人工审核平行，不确定的转人工（`escalated`）。

```
① 采集入库
② LLM 抽取（NER + 初步 NEL）→ extracted_entities/relations（review_status=pending）
        │
        ▼
③ 审核  ┌─────────────────────────────────────────────────────────┐
        │ Agent A 实体审核 ──┐                                      │
        │ Agent A' 关系审核 ─┤ 自动审核 pending 实体/关系             │
        │   （低置信度/不确定 → escalated 转人工审核工作台）          │
        │ Agent D 实体合并消歧：对已审核(approved)实体 → 链接 dict   │
        │                       → 生成 identity_merge_proposal      │
        └─────────────────────────────────────────────────────────┘
        │
        ▼
④ 实体归并 ┌───────────────────────────────────────────────────────┐
        │ Agent C 语义匹配：判断两个 entity_dict 是否同一实体         │
        │ Agent B 冲突裁决：处理 entity_merge_queue 属性冲突          │
        │   （+ 人工：MergeWorkbench / FieldConflict / 提案确认）     │
        └───────────────────────────────────────────────────────┘
        │
        ▼
⑤ 推送下游（WeKnora RAG + Neo4j 图投影）
```

5 种 Agent 分布在两个环节：

| Agent | 所在环节 | 触发对象 |
|---|---|---|
| A 实体审核 | ③ 审核 | `extracted_entities.review_status=pending` |
| A' 关系审核 | ③ 审核 | `entity_relations.review_status=pending` |
| D 实体合并消歧 | ③ 审核（衔接④） | 已审核通过、未链接 dict 的实体 |
| C 语义匹配 | ④ 实体归并 | 两个 `entity_dict` 是否同实体 |
| B 冲突裁决 | ④ 实体归并 | `entity_merge_queue` 属性冲突 |

> Agent A/A'/D 属于**审核自动化**（决定实体/关系通过、拒绝、或链接到哪个实体）；Agent B/C 属于**归并辅助**（解决多来源合并时的冲突与身份判定）。审核通过后才进入归并，归并完成后才推送下游。

### 7.2 与消歧 Step 6 的关系（自动化与质量的权衡）

消歧 Step 6（`disambiguation/llm_ranker.py::rank_with_llm`，在 `engine.py:146-162` 被注释禁用）是**抽取链路内嵌的 NEL 自动精排兜底**：当 Reranker 无法确定时，用一次轻量 `qwen_chat` 从候选里判断链接到 `candidate_N` 或 `none`，**自动落锤写 `dict_id`，无工具、无 ReAct、无人工确认**。

Agent 体系与 Step 6 的关系是**「职责迁移 + 增强」，而非「放大」**：

- **范围**：Step 6 只做「NEL 精排链接」一件事；Agent 体系做了 5 件事，其中只有 **Agent D（实体合并消歧）** 与 Step 6 目标重叠，Agent A/A'/B/C 是全新职责。
- **性质**：Step 6 是「在线同步、自动落锤」；Agent D 是「离线异步、生成 `identity_merge_proposal` 提案 + 人工确认」。
- **时机**：Step 6 在抽取时同步触发；Agent D 在审核阶段触发，可异步、可批量、SSE 流式。

因此可以概括为：**Step 6 是「快但糙」的自动挡，Agent 审核是「慢但稳」的手动挡——用吞吐换质量。**

- 「更谨慎、质量更高」来自三个机制叠加：ReAct 多步推理（而非单次调用）、工具检索（`exact_search`/`fuzzy_search`/`get_entity_detail`）、人工确认闸门（提案 approve/reject + `escalated` 转人工）。
- 「牺牲自动化」体现在：从「抽取时无人落锤」退化为「离线异步 + 审核通过 + 提案确认」两道人工闸门。

**注意**：质量提升是**预期收益而非必然结果**，前提是人工确认环节真正执行；若人工直接批量 approve，最后防线被架空，质量增益会回落。该设计的质量上限取决于审核者认真程度。

**结论**：当前「质量优先、人工兜底」定位下，Step 6 可有可无，Agent D 是更可靠的替代（这正是注释掉 Step 6 的原因）；但 Step 6 保留的「无人化自动落锤」能力是 Agent D 无法替代的，一旦系统追求全自动高吞吐，可恢复 Step 6 并加高置信度阈值（如仅 `confidence ≥ 0.9` 才自动落锤）以平衡质量。

---

## 8. 前端架构（`frontend/src/`）

路由 14 个页面（懒加载）：

| 路由 | 页面 | 用途 |
|---|---|---|
| `/overview` | PipelineOverview | 数据面板：6 阶段流水线总览 + 站点分布扇形图 |
| `/documents` | DocumentList | 文档列表 + 批量抽取/重爬/上传 MD |
| `/documents/:uuid` | DocumentDetail | 文档详情 + Cytoscape 单文档图谱 |
| `/review/:uuid` | ReviewWorkbench | 单文档人工审核 |
| `/batch-review` | BatchReviewWorkbench | 多文档批量审核 |
| `/graph-review` | GraphReview | 全量图谱可视化审核（G6，功能最丰富） |
| `/entity-merge` | EntityMerge | 实体消歧（SSE 流式 AI 推理） |
| `/review/merge-workbench` | MergeWorkbench | 实体身份手动合并 |
| `/review/field-conflict` | FieldConflict | 属性冲突裁决 |
| `/review/identity-merge` | IdentityMergeProposals | Agent 身份合并提案确认 |
| `/neo4j` | Neo4jManager | Neo4j 图数据推送 |
| `/weknora` | WeknoraManager | WeKnora 知识库同步 |
| `/agent` | AutoReviewDashboard | Agent 运行记录仪表盘 |
| `/agent/document-review` | DocumentReview | 文档维度 Agent 审核 |

### 8.1 流程全景对照（数据状态 × 前端动作 × 数据表）

完整业务闭环，横跨「数据状态变化 + 前端在哪操作 + 数据落在哪张表」：

```
① 抽取（documents）
   数据状态：extraction_status = pending → queued → processing → done/failed/no_entity
   前端动作：文档列表 /documents「批量抽取 / 批量重抽 / 暂停继续」
   （内部：LLM 抽取 → 6 step 初步消歧 → 写 extracted_entities，dict_id 部分已填）
        │
        ▼
② 实体审核（extracted_entities.review_status）
   人工：审核工作台 /review/:uuid、批量审核 /batch-review、图谱审核 /graph-review
     · 通过 approve → approved / 拒绝 reject → rejected / 选候选 dict_id
   Agent：自动审核 /agent、文档审核 /agent/document-review
     · auto_approved（Step2 交叉验证自动通过）/ 转人工 escalated
        │
        ▼
③ 关系审核（entity_relations.review_status）
   前端动作：审核工作台「关系」、图谱审核 → 通过 approve / 拒绝 reject
        │ 审核通过
        ▼
④ 消歧链接（dict_id 补写）
   前端动作：实体消歧 /entity-merge（Agent D SSE 流式）、身份合并提案 /review/identity-merge（确认链接/拒绝/强制新建）
        │
        ▼
⑤ 归并（entity_dict 层）
   前端动作：实体身份合并 /review/merge-workbench（搜索→对比→Keeper→合并）、属性冲突 /review/field-conflict（采用新值/保留/自定义）
        │
        ▼
⑥ 同步（出口）
   前端动作：WeKnora 同步 /weknora（文档推知识库）、Neo4j 图推送 /neo4j（实体/关系推图库）
```

**前端动作 → 数据字段变化对照**：

| 环节 | 前端页面 | 动作 | 改变的字段 |
|---|---|---|---|
| ② 实体审核 | 审核工作台 / 批量审核 / 图谱审核 | 通过 / 拒绝 | `extracted_entities.review_status` → approved / rejected |
| ② Agent 审核 | 自动审核 / 文档审核 | 自动通过 / 转人工 | `review_status` → auto_approved / escalated |
| ③ 关系审核 | 审核工作台「关系」/ 图谱审核 | 通过 / 拒绝 | `entity_relations.review_status` → approved / rejected |
| ④ 消歧 | 实体消歧页 | Agent 流式消歧 | 生成 `entity_identity_merge_proposal` |
| ④ 消歧确认 | 身份合并提案页 | 确认链接 / 拒绝 | `extracted_entities.dict_id` + proposal.status |
| ⑤ 归并 | 实体身份合并页 | 合并字典实体 | `entity_dict` + `entity_merge_log` |
| ⑤ 冲突裁决 | 属性冲突处理页 | 字段裁决 | `entity_dict.canonical_data` + `entity_merge_queue.resolution` |
| ⑥ 同步 | WeKnora / Neo4j 页 | 触发同步 | `documents.weknora_sync_status` / `extracted_entities.weknora_graph_status` |

**数据面板逐阶段 → 表 → 字段对照**（`GET /api/agent/pipeline/overview`，仅读 7 张表）：

| 阶段 | 数据项 | 表 | 字段 | 条件 |
|---|---|---|---|---|
| ① 抽取 | pending/queued/processing/done/failed/no_entity | `documents` | `extraction_status` | `is_deleted=false` |
| ① 站点分布 | pending/done 各站点数（`by_site` 嵌套）| `documents` | `source_site` | `extraction_status IN ('pending','done')` |
| ② 实体审核 | pending/auto_approved/approved/rejected | `extracted_entities` | `review_status` | `is_deleted=false` |
| ② 站点分布 | 各站点待审核实体数（`by_site` 嵌套）| `extracted_entities` | `source_site` | `review_status='pending'` |
| ③ 关系审核 | pending/approved/rejected | `entity_relations` | `review_status` | `is_deleted=false` |
| ④ 消歧链接 | linked / unlinked | `extracted_entities` | `dict_id IS (NOT) NULL` | `is_deleted=false` |
| ④ 匹配分布 | 各 match_step 数（`by_match_step`）| `extracted_entities` | `entity_data->_disambig->match_step` | `is_deleted=false` |
| ⑤ 归并 | proposal_pending/approved/rejected | `entity_identity_merge_proposal` | `status` | — |
| ⑤ 归并 | conflict_pending | `entity_merge_queue` | `resolution='pending'` | — |
| ⑤ 字典现状 | PrimeKG/抽取 字典实体数（`by_dict`）| `entity_dict` | `is_verified` | `is_deleted=false` |
| ⑤ 字典现状 | 强ID重复组数（`by_dict`）| `entity_dict` | `external_ids` 强ID字段按 `entity_type` 分组 count>1 | `is_deleted=false` |
| ⑥ 同步 | 文档 weknora_pending/synced/failed | `documents` | `weknora_sync_status` | `is_deleted=false` |
| ⑥ 同步 | 实体 graph_entity_pending/pushed/failed | `extracted_entities` | `weknora_graph_status` | `is_deleted=false` |
| ⑥ 同步 | 关系 graph_relation_pending/pushed/failed | `entity_relations` | `weknora_graph_status` | `is_deleted=false` |

> 2026-09-21 补齐了三处：① 消歧链接卡加 `match_step` 分布（消歧引擎健康度仪表盘）；② 归并卡加「字典现状」（entity_dict 总量 + 强ID重复组，解决原「字典层盲区」）；③ 同步卡区分「文档同步/实体推送/关系推送」三路。数据面板从读 5 张表扩展到 7 张表（新增 `entity_dict`、`entity_relations`）。

**实体审核存量统计**（`GET /api/agent/entity-review-stats`，自动审核页顶部两卡）：

| 数据项 | 表 | 字段 | 条件 |
|---|---|---|---|
| 待审核实体 | `extracted_entities` | `review_status` | `='pending'` |
| 已审核实体 | `extracted_entities` | `review_status` | `IN ('approved','auto_approved','rejected')` |

---

## 9. 现有基础盘点（已具备的能力）

1. **采集能力完整**：19 个药学数据源（NMPA/CDE/FDA/CHICTR/ClinicalTrials/PubMed/DrugBank/NCCN/HKEX/FiercePharma/PHIRDA 等），支持网页/PDF(MinerU)/Excel/DOCX/Markdown 上传。
2. **抽取链路成熟**：16 类实体 + 25 类关系，来源专属 prompt 模板，智能分块 + 跨块去重，可插拔任务队列。
3. **消歧引擎**：强 ID + 别名 + 规则 + pgvector 向量召回 + Reranker 精排，六阶段流水线。
4. **审核体系完善**：单文档/批量/图谱三种审核入口，实体/关系/合并冲突全覆盖，元数据编辑。
5. **实体字典与归并**：canonical 数据合并规则（数组追加、状态优先级覆盖、快照时间覆盖）、属性冲突队列、身份合并 + 快照回滚。
6. **Agent 自动审核**：5 种 Agent，Pipeline + ReAct 两种编排，联网检索工具，SSE 流式推理可视化。
7. **下游同步**：WeKnora 全文 RAG + Neo4j 图投影，状态可追踪、可重试。
8. **审计与运维**：review_log + operation_log 双审计，crawl_log 爬取日志，迁移器 + 15 个版本迁移，DB 连接池/Worker/队列状态监控端点。
9. **前端体验**：统一 Teal 主题、图可视化（G6/Cytoscape）、统计卡片、推理时间线等。

---

## 10. 待完善的地方（gap / 技术债）

### 10.1 工程质量

- **测试几乎为空**：`tests/` 仅有 `conftest.py` 的 mock fixtures 和空的 `__init__.py`，**无任何实际测试用例**（`test_*.py`）。消歧、合并、审核、抽取等核心逻辑缺少回归保障，尤其是「软删除 + 无外键」架构下数据完整性高度依赖 repository 事务逻辑，风险较高。
- **CORS 配置偏松**：`allow_origins` 含 `"*"` 且 `allow_credentials=True`，生产环境需收紧为白名单。

### 10.2 功能缺口 / 已知限制

- **Step 4 向量召回需有效 embedding key**：数据库侧（`name_embedding` 列 + pgvector 扩展，V015 迁移）已就绪，但 `.env` 中 `LLM_API_KEY=dummy` 导致 `generate_embedding` 401 失败，向量召回 + Reranker 精排链路仍未真正产出候选，NEL 实际只有 Step 1/2/3 规则与精确匹配在生效。
- **消歧 Step6 LLM 兜底被禁用**：`ambiguous` 实体直接按 Reranker 结果处理，不触发 LLM 精排。**方案决策**：Step6 不进入 Baseline，降级为分级路由的「中置信度复核」（仅 0.7–0.9 区间触发），避免全量兜底的延迟代价（相关代码已就绪，仅需启用）。
- **任务队列默认 `memory`**：重启即丢任务；生产需切 `TASK_QUEUE_BACKEND=redis` 并部署独立 RQ worker 进程。
- **本地 PDF 批量入口未启用**：`LOCAL_PDF_SCAN_DIRS` 为空，本地 PDF 目录扫描解析功能未接通。
- **交叉验证自动通过**（`auto_approved`）机制：同一 `dict_id` 二次命中即自动通过，缺少置信度门槛，存在误合并放行风险，建议加人工抽检或阈值。

### 10.3 文档与一致性

- **README.md 严重滞后**：只覆盖早期 ETL 爬虫部分，未涵盖抽取、消歧、审核、合并、Agent、图同步等已实现的大量能力。
- **`dev_plan.md` 与现状脱节**：为 2026-05 的规划文档，部分模块描述（如"repository 是空 placeholder"）已与实际不符，且模块边界与当前实现有出入。
- **`GRAPH_SYNC_DESIGN.md` 的定位需更新**：其描述的是「上游数据库 + Neo4j」的理想边界，但当前项目实际通过 **WeKnora 中转**推送 Neo4j 拷贝库，两套叙事需统一。
- **`review_exporter.py` docstring 与实际状态值不一致**（`PENDING_MANUAL_REVIEW` vs `PENDING_REVIEW`），需核对校正。

### 10.4 目录边界待厘清

- `frontend-next/`（Next.js 前端）、`WeKnora-main/`（docreader + mcp-server）、`Weknora-frontend/`（空目录）与项目主力 `frontend/`（Vue3）的关系和去留需明确，README 未说明。

### 10.5 建议的优先级

| 优先级 | 事项 | 原因 |
|---|---|---|
| P0 | 补齐核心链路单元/集成测试 | 数据完整性靠代码约束，缺回归保障风险最大 |
| P1 | 明确生产部署形态（Redis 队列 + RQ worker） | 当前 memory 队列无法用于生产 |
| P1 | 消歧 Step6 降级为中置信度复核（0.7–0.9 触发，≥0.9 自动落锤） | 直接关系消歧质量 |
| P1 | 配置有效 embedding key，打通 Step 4 向量召回 | 当前向量召回链路空转，NEL 仅规则/精确匹配生效 |
| P2 | 更新 README / GRAPH_SYNC_DESIGN 文档 | 消除新成员上手误导 |
| P3 | 收紧 CORS、交叉验证加阈值/抽检 | 安全与数据质量加固 |

---

## 附录

- 实体类型（16）：Company、Drug、Target、Indication、ClinicalTrial、DealEvent、DealItem、ApprovalEvent、Policy、SafetyEvent、RecallEvent、TCMFormula、Compound、Pathway、DevelopmentProject、TrialIndication。
- 关系类型（25）：DEVELOPS、PARTICIPATES_IN、INVESTED_IN、SUBSIDIARY_OF、SPONSORS、ISSUES、APPROVES、PARTICIPATES_IN_PROJECT、TARGETS、TREATS、IN_TRIAL、HAS_APPROVAL、HAS_ITEM、INVOLVES_DRUG、EVALUATES、FOR_INDICATION、IN_PATHWAY、ASSOCIATED_WITH、HOMOLOG_OF、SUBTYPE_OF、AFFECTS、DEVELOPED_BY、CONTAINS、RELATED_TO 等。
- API 端点共 **89 个**：documents 7、extraction 8、review 27、weknora 10、agent 21、main 1（`/health`）。

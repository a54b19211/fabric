# 成员5 需求文档 — 前端

## 你负责什么

前端所有页面（`frontend/` 目录）。接口全部调成员4提供的 FastAPI，不需要写任何后端代码。

爬取监控直接用 Prefect 自带 Dashboard，不需要你开发。

---

## 背景：你的工作在流程图中的位置

```
第一阶段（数据查看 + 触发抽取）← 你提供操作界面
  └─ 文档列表页：查看已入库文档，触发 LLM 抽取
  └─ 文档详情页：查看原文全文，确认后触发抽取

第四阶段（审核消歧）← 你提供操作界面
  └─ 审核工作台：展示实体/关系，支持通过/拒绝

第五阶段（推图）← 你提供操作界面
  └─ Neo4j 推送管理页：查看推送状态，触发推送/重试

并行下游（RAG）← 你提供操作界面
  └─ WeKnora 同步管理页
```

---

## 技术选型

用你熟悉的框架即可（React / Vue 均可）。所有接口调成员4的 FastAPI，本地开发时用 `http://localhost:8000`，成员4会配好 CORS。

---

## 页面清单

### 1. 文档列表页（/documents）

展示所有已入库的文档，支持筛选、查看原文和触发抽取。

| 列 | 说明 |
|----|------|
| 标题 | 可点击跳转文档详情页 |
| 来源站点 | nmpa/cde/pubmed 等 |
| 发布日期 | YYYY-MM-DD |
| 置信度 | 0~1 的数字，低于 0.8 标红 |
| 文档状态 | AUTO_PROCESSED / PENDING_REVIEW / REVIEWED |
| 抽取状态 | pending / processing / done / failed，带颜色标识 |
| 操作 | 查看原文 / 触发抽取 |

筛选条件：
- 来源站点（下拉选择）
- 文档状态（下拉选择）
- 抽取状态（下拉选择）
- 发布日期范围（日期选择器）

**抽取状态自动刷新**：
- 页面顶部显示统计卡片：待抽取 N 条 / 抽取中 N 条 / 已完成 N 条 / 失败 N 条
- 统计数据调 `GET /api/extraction/stats`
- 当有文档处于 `processing` 状态时，每 5 秒自动轮询一次刷新统计和列表
- 无 processing 状态时停止轮询

**触发抽取操作**：
- 单条触发：每行有"触发抽取"按钮，仅对 `extraction_status=pending` 的文档显示
- 批量触发：表格顶部有"批量触发抽取"按钮，对当前筛选结果中所有 pending 的文档触发
- 触发后该行抽取状态立即变为 `processing`（乐观更新），等待轮询刷新真实状态

---

### 2. 文档详情页（/documents/{uuid}）

用户在列表页点击"查看原文"后跳转，展示文档完整内容。

**页面布局**：
- 顶部：文档基本信息（标题、来源、发布日期、置信度、抽取状态）
- 中部：原文全文（Markdown 渲染）
- 底部操作栏：
  - 若 `extraction_status=pending`：显示"触发 LLM 抽取"按钮
  - 若 `extraction_status=done`：显示"查看抽取结果"按钮，跳转到审核工作台页 `/review/{uuid}`
  - 若 `extraction_status=failed`：显示错误原因 + "重新触发"按钮

**调用接口**：
- `GET /api/documents/{uuid}`
- `POST /api/extraction/trigger`

---

### 3. 审核工作台页（/review/{uuid}）

左右分栏布局。

**左侧：原文展示**
- Markdown 渲染原文全文
- 高亮显示 `source_text` 对应的原文片段
- 鼠标悬停在右侧实体卡片时，左侧对应片段高亮

**右侧：实体和关系列表**

实体卡片显示：
- 实体类型、实体名称、置信度
- `entity_data` 关键字段（展开/折叠）
- 消歧状态标签：🟢 精确匹配 / 🟡 向量匹配建议 / 🔵 新实体
- 操作按钮：✅ 通过 / ❌ 拒绝

关系卡片显示：
- 关系类型、起点 → 终点、置信度
- 操作按钮：✅ 通过 / ❌ 拒绝

**顶部操作栏**：
- 一键全部通过（置信度 >= 0.8 的）
- 一键全部拒绝
- 审核备注输入框

**调用接口**：
- `GET /api/review/entities`
- `POST /api/review/entities/{id}/approve`
- `POST /api/review/entities/{id}/reject`
- `GET /api/review/relations`
- `POST /api/review/relations/{id}/approve`
- `POST /api/review/relations/{id}/reject`

---

### 4. Neo4j 推送管理页（/neo4j）

**顶部统计卡片**（调 `GET /api/neo4j/stats`）：
- 实体：待推送 N / 已推送 N / 失败 N
- 关系：待推送 N / 已推送 N / 失败 N

**操作按钮**：
- 批量推送实体（调 `POST /api/neo4j/sync/entities`）
- 批量推送关系（调 `POST /api/neo4j/sync/relations`）

**失败记录列表**（调 `GET /api/neo4j/failed`）：
- 展示失败的实体/关系，显示错误原因
- 每条有"重试"按钮（调 `POST /api/neo4j/retry`）

---

### 5. WeKnora 同步管理页（/weknora）

**顶部统计卡片**（调 `GET /api/weknora/stats`）：
- 待同步 N 条 / 已同步 N 条 / 失败 N 条

**文档列表**（调 `GET /api/weknora/pending`）：

| 列 | 说明 |
|----|------|
| 标题 | 文档标题 |
| 来源站点 | |
| 发布日期 | |
| 同步状态 | pending / synced / failed |
| 失败原因 | 仅 failed 时显示 |
| 操作 | 手动触发同步 / 重试 |

**操作**：
- 批量同步按钮（调 `POST /api/weknora/sync-batch`）
- 单条重试按钮（调 `POST /api/weknora/sync/{uuid}`）

---

## 注意事项

1. 审核工作台的高亮功能：`source_text` 是原文的子字符串，用字符串匹配在 Markdown 渲染后的文本里找到对应位置高亮
2. 消歧状态标签来自 `extracted_entities.disambiguation_status` 字段

---

## 与其他成员的依赖

- 依赖成员4的所有 API 接口（Day 1 冻结 endpoint 路径和响应结构）

---

## 交付标准

- 5 个页面全部实现
- 审核工作台的原文高亮功能可用
- 抽取状态轮询刷新正常工作
- 与成员4联调通过

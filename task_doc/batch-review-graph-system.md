# 批量审核 + 图谱可视化系统

## 设计原则

- 底层 repository.py / disambiguation 不改，新增集中在 API 聚合层 + 前端
- 已审核和待审核实体均可展示、可编辑元数据（entity_data），不可改 entity_name
- 批量操作复用现有 batch_approve_entities / batch_reject_entities

---

## 一、数据模型与状态机

```
extracted_entities 状态流转：

  pending ──→ approved ──→ pushed (WeKnora/Neo4j)
    │            │
    └─ rejected  │  均可修改 entity_data
                 │  均可出现在图谱中
                 
entity_relations 同理：
  pending ──→ approved ──→ pushed
    │
    └─ rejected（实体拒绝时级联）
```

图谱展示范围：`review_status IN ('pending', 'approved') AND is_deleted = FALSE`

---

## 二、新增 API

### 2.1 图谱数据接口

`GET /api/review/graph`

参数：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| entity_types | string | "" | 逗号分隔，空=全部 |
| source_site | string | "" | 来源站点筛选 |
| review_status | string | "pending,approved" | 逗号分隔 |
| source_doc_uuid | string | "" | 单文档 UUID（可选，用于文档维度查看） |
| include_relations | bool | true | 是否包含关系边 |
| limit | int | 500 | 最大节点数 |

返回：
```json
{
  "nodes": [
    {
      "id": 123,
      "entity_type": "Drug",
      "entity_name": "阿司匹林",
      "entity_data": { ... },
      "dict_id": 5,
      "review_status": "pending",
      "source_doc_uuid": "xxx",
      "source_site": "cde",
      "confidence_score": 0.92,
      "is_verified": true       // entity_dict.is_verified
    }
  ],
  "edges": [
    {
      "id": 456,
      "relation_type": "treats",
      "from_entity_id": 123,
      "to_entity_id": 789,
      "review_status": "pending",
      "relation_props": { ... },
      "source_doc_uuid": "xxx"
    }
  ]
}
```

实现：直接查 extracted_entities + entity_relations，JOIN entity_dict 拿 is_verified，不做额外聚合。

### 2.2 批量审核（复用现有）

`POST /api/review/entities/batch/approve` — 已存在
`POST /api/review/entities/batch/reject` — 已存在
`POST /api/review/relations/batch/approve` — 已存在
`POST /api/review/relations/batch/reject` — 已存在

前端传入 entity_ids / relation_ids 列表即可。

### 2.3 实体元数据修改

`PATCH /api/review/entities/{entity_id}`

```json
{
  "entity_data": {
    "indication": "高血压",
    "aliases": ["别名1", "别名2"]
  }
}
```

约束：
- 不可修改 entity_name、entity_type
- 不可修改 review_status（走审核接口）
- entity_data 做 shallow merge：传入的 key 覆盖，未传入的保留
- 已 pushed 的实体修改后重置 weknora_graph_status 为 pending

### 2.4 关系元数据修改

`PATCH /api/review/relations/{relation_id}`

```json
{
  "relation_props": { "dosage": "10mg" }
}
```

约束同上。

### 2.5 触发单文档重抽取

`POST /api/extraction/re-extract/{uuid}`

约束：
- 文档 extraction_status 不能是 "processing"（正在抽取中）
- 重置 extraction_status = "processing" → 提交到 WorkerManager
- 旧的 extracted_entities / entity_relations 不删除（review_status 为 pending/approved/rejected 的记录保留，下次抽取只是新增行 + 自动去重合并）

### 2.6 待审核统计（仪表盘用）

`GET /api/review/stats`

```json
{
  "entities": { "pending": 42, "approved": 150, "rejected": 8 },
  "relations": { "pending": 35, "approved": 120, "rejected": 5 },
  "by_type": {
    "Drug": { "pending": 20, "approved": 80 },
    "Disease": { "pending": 15, "approved": 50 }
  },
  "by_site": {
    "cde": { "pending": 30, "approved": 100 },
    "nmpa": { "pending": 10, "approved": 40 }
  }
}
```

---

## 三、前端页面结构

### 3.1 图谱审核页（核心）

```
┌─────────────────────────────────────────────────┐
│  筛选栏：实体类型 ▼  来源站点 ▼  状态 ▼  文档UUID │
│  [批量通过] [批量拒绝]             节点数: 150   │
├────────────────────┬────────────────────────────┤
│                    │                            │
│    图谱可视化       │   详情面板                  │
│    (力导向图)      │   entity_name              │
│                    │   entity_type              │
│    ○ ── ○         │   review_status            │
│    │    │          │   entity_data (可编辑)      │
│    ○    ○         │   ────────────              │
│    │              │   关联关系列表               │
│    ○              │   [通过] [拒绝] [编辑]      │
│                    │                            │
├────────────────────┴────────────────────────────┤
│  选中: 3 个实体, 5 条关系  [批量通过] [批量拒绝]  │
└─────────────────────────────────────────────────┘
```

### 3.2 图谱操作

- **点选节点** → 右侧显示详情 + 关联关系
- **框选/Shift多选** → 批量操作
- **节点颜色**：pending=橙色, approved=绿色, rejected=灰色
- **边样式**：pending=虚线, approved=实线
- **双击节点** → 展开关联实体（类似 Neo4j Browser 的展开模式）
- **拖拽节点** → 不会触发 merge（merge 走审核页，不做图谱拖拽合并）

### 3.3 详情面板

选中实体后显示：
- 基本信息（不可编辑）：entity_name, entity_type, confidence_score, source_site
- 元数据（可编辑）：entity_data 中的各字段，按类型渲染表单
- dict_id 链接：可跳转到 entity_dict 详情
- 关联关系列表：每条关系有 [通过] [拒绝] 按钮
- 操作按钮：[审核通过] [审核拒绝] [保存修改]

---

## 四、实施计划

| 阶段 | 内容 | 依赖 |
|------|------|------|
| Phase 1 | 聚合 API：graph、stats、entity/relation PATCH | 无 |
| Phase 2 | 图谱可视化页面（力导向图 + 筛选栏） | Phase 1 |
| Phase 3 | 详情面板 + 元数据编辑 | Phase 1 |
| Phase 4 | 批量操作（框选/多选 → 批量通过/拒绝） | Phase 1 |
| Phase 5 | 单文档重抽取 + 文档维度筛选 | Phase 1 |

---

## 五、不变项

- `db/repository.py` 审核逻辑不改
- `disambiguation/` 消歧流水线不改
- `flows/extraction_flow.py` 抽取流程不改
- entity_dict 创建/合并逻辑不改
- WeKnora/Neo4j 同步触发逻辑不改

改动集中在：
- `api/routers/review.py` — 新增 graph、stats、PATCH 端点
- 前端新增图谱审核页（不替换现有逐文档审核页，作为可选入口）

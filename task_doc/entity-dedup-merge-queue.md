# 实体去重：同 dict_id 只保留一条 extracted_entities

## 问题

1. 同一实体被多篇文档抽取，消歧 auto_matched 到同一 dict_id，但 `_find_existing_entity_by_dict_id` 要求 `review_status='approved'`，pending 期间找不到 → 全插新行 → dict_id 下挂多条 extracted_entities
2. 合并 entity_data 时用 `_merge_jsonb_entity_data` 静默合并，跳过了 `entity_merge_queue`
3. 审核通过后没有去重，重复行一直存在

## 目标

- 一个 dict_id 最终只对应一条 extracted_entities（最老的那条）
- 所有 entity_data 合并经过 `process_entity_merge` → `entity_merge_queue`

---

## 改动

### 1. `_find_existing_entity_by_dict_id` — 去掉 approved 条件

**文件**: `db/repository.py`

**现状**:
```python
.filter(
    ExtractedEntity.dict_id == dict_id,
    ExtractedEntity.entity_type == entity_type,
    ExtractedEntity.review_status == "approved",  # ← 这行
    ExtractedEntity.is_deleted == False,
)
.order_by(ExtractedEntity.id.asc())
.first()
```

**改为**: 去掉 `review_status` 条件，直接找该 dict_id 下最老的未删除行。

**理由**: auto_matched 来自 Step1-3 精确匹配，确定性足够高，不需要等审核通过才合并。

---

### 2. 合并 entity_data 统一走 `process_entity_merge`

**文件**: `db/repository.py` → `_do_insert_extraction_result` Phase 3

**现状**: 直接调 `_merge_jsonb_entity_data` 静默合并到 `extracted_entities.entity_data`

**改为**: 调 `process_entity_merge` 合并到 `entity_dict.canonical_data`，然后 `extracted_entities.entity_data = entity_dict.canonical_data`

**规则**（`process_entity_merge` 已有）:
- 空字段补充 → auto merge
- 数组追加去重 → auto merge
- 快照字段新时间覆盖 → auto merge
- 高优来源覆盖 → auto merge
- 同优先级不同源 → 进 `entity_merge_queue` 人工审核

**效果**: 冲突不再静默吞掉，前端 merge-queue 可见可操作。

---

### 3. 审核通过后去重同 dict_id 实体

**文件**: `db/repository.py` → `approve_entity_with_alias_writeback` 末尾

**新增函数**: `_dedup_extracted_entities_by_dict_id(session, dict_id, keeper_id)`

**逻辑**:
```
1. 查同 dict_id 下的所有其他行（id != keeper_id, is_deleted=false）
2. 对每一行 duplicate:
   a. entity_name → write_alias(session, dict_id, name)
   b. entity_data → process_entity_merge(session, dict_id, keeper_id, entity_data, source_site)
      - 能自动合的 → 写入 canonical_data
      - 冲突的 → 写入 entity_merge_queue（pending），不阻塞去重
   c. EntityRelation where from/to = duplicate.id → 重定向到 keeper_id
   d. duplicate.is_deleted = true
   e. log_review_log("entity", duplicate.id, "dedup_merge", ...)
3. 记录去重数量

冲突不影响去重，merge_queue 已留存 existing_value/new_value/source_site，
前端 merge-queue 面板可正常 resolve。
```

---

## 调用关系

```
extraction_flow.py
  └─ _do_insert_extraction_result
       ├─ Phase 1: 分类（auto_matched + 已有行 → merge，否则 insert）
       ├─ Phase 2: 插入新行
       ├─ Phase 3: 合并 entity_data → process_entity_merge ✅ 改为调这个
       ├─ Phase 4: 写关系
       └─ Phase 5: 更新文档状态

review (人工审核)
  └─ approve_entity_with_alias_writeback
       ├─ alias 回写（已有）
       ├─ process_entity_merge（已有）
       └─ _dedup_extracted_entities_by_dict_id ✅ 新增
```

---

## 存量数据处理

历史数据中同 dict_id 已有多行的情况，提供清理脚本：

```sql
-- 每个 dict_id 保留 id 最小的，其他软删除
UPDATE extracted_entities SET is_deleted = true
WHERE id NOT IN (
    SELECT MIN(id) FROM extracted_entities
    WHERE dict_id IS NOT NULL AND is_deleted = false
    GROUP BY dict_id
) AND dict_id IS NOT NULL AND is_deleted = false;
```

---

## 不变的部分

- 关系写入逻辑不变（final_id_map 已正确处理 merge 场景）
- `process_entity_merge` 内部合并规则不变
- `entity_merge_queue` 结构和后端 API 不变
- 非 auto_matched 实体（candidate_matched / ambiguous / new_entity）仍然插入新行

---

## 前端：GraphReview 加 merge_queue 面板

`GraphReview.vue` 当前没有 merge queue 展示，去重冲突产生后用户无感知。

### 入口
- 图谱右上角加一个「合并队列 (N)」徽章按钮，N 为 pending 冲突数
- 点击后在详情面板区域切换为 merge_queue 列表
- 或者右上角弹出 Drawer

### 面板内容
复用 `ReviewWorkbench.vue` 的 merge-queue Tab 结构：
- 冲突字段名 → 现有值 vs 新值
- 操作按钮：「采用新值」「保留现有」
- 调用已有 API：`GET /api/review/merge-queue`、`POST /api/review/merge-queue/{id}/resolve`

### API（已有，无需新增）
- `getMergeQueue(limit)` — 查询 pending 冲突列表
- `resolveMerge(mergeId, reviewer, resolution, resolvedValue)` — 解决冲突

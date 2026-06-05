# 实体去重 v2：首次抽取即建 entity_dict

## 问题

当前消歧只查 `is_verified=True` 的 entity_dict。两个 Worker 并行处理文档 A 和 B，
同时抽取同一实体（如"阿司匹林"），都判定为 `new_entity`，各自插入 extracted_entities，
导致同一实体有多条 pending 记录。

更复杂的情况：文档 A 抽到"恒瑞医药"，文档 B 抽到"江苏恒瑞"——即使串行，
B 的消歧也无法匹配到 A 创建的 pending extracted_entities（消歧不走 extracted_entities 表）。

## 方案概述

**核心思路**：首次抽取实体时立即创建 entity_dict（`is_verified=False`），
让后续消歧可以匹配到。审核通过时改为 `is_verified=True`，WeKnora 只同步已审核实体。

**三个关键改动**：

1. **entity_dict 提前创建** — insert 阶段对 new_entity 自动建 entity_dict（is_verified=False）+ aliases
2. **串行锁** — 消歧 + entity_dict 创建 + 入库整个临界区加 `threading.Lock`
3. **审核页动态查询** — 不再依赖预计算的 `_disambig.candidates`，改为实时查 entity_dict

---

## 改动清单

### 1. `db/repository.py`

#### 1.1 `get_entity_dict_for_weknora` — 加 `is_verified=True` 过滤

当前漏了此条件，auto-created 的 entity_dict 会被推送 WeKnora。加一行过滤。

#### 1.2 `_create_entity_dict_from_extraction` — 支持 `is_verified` 参数

签名加 `is_verified: bool = True`，调用处传 `is_verified=False`。
现有调用方（approve_entity_with_alias_writeback）不受影响（默认 True）。

#### 1.3 Phase 2 改造 — new_entity 自动建 entity_dict

在 `_do_insert_extraction_result` Phase 2，对 insert 实体做判断：

```
if dict_id is None and _disambig.status == "new_entity" and entity_name:
    cache_key = (entity_type, entity_name)
    if cache_key in batch_dict_cache:
        dict_id = batch_dict_cache[cache_key]      # 同一批次内复用
    else:
        dict_id = _create_entity_dict_from_extraction(..., is_verified=False)
        batch_dict_cache[cache_key] = dict_id
        add_aliases_no_embedding(...)              # 写别名（无 HTTP）
    rec["dict_id"] = dict_id
```

`batch_dict_cache` 防止同一文档内"阿司匹林"出现两次时创建重复 entity_dict。

#### 1.4 新增软删除函数（PrimeKG 保护）

```python
def soft_delete_entity_dict(dict_id: int, operator: str = "") -> None:
    # canonical_source == "primekg" → raise ValueError
    # 同时软删所有 entity_aliases

def soft_delete_entity_alias(alias_id: int, operator: str = "") -> None:
    # 所属 entity_dict.canonical_source == "primekg" → raise ValueError
    # is_canonical == True → raise ValueError（标准名不可删）
```

#### 1.5 新增动态候选查询函数

```python
def find_candidate_dicts_for_review(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
    top_k: int = 10,
) -> list[dict]:
    """
    审核页实时查询 entity_dict + entity_aliases，返回候选实体列表。
    - 不过滤 is_verified（未审核的 entity_dict 也要展示）
    - 查询路径：别名精确匹配 → 向量相似度 → 合并去重
    - 返回 [{dict_id, entity_type, name, aliases, canonical_source, 
              is_verified, match_type, score}, ...]
    """
```

### 2. `flows/extraction_flow.py`

#### 2.1 串行锁

模块级 `_disambig_insert_lock = threading.Lock()`，包裹消歧 + 入库：

```python
with _disambig_insert_lock:
    with get_session() as session:
        entities = _disambiguate(entities, session=session)
        ...
        insert_extraction_result(..., session=session)
```

锁的粒度：每个文档的消歧+入库。LLM 抽取（最耗时部分）在锁外并行，不受影响。

### 3. `api/routers/review.py`

#### 3.1 新增 API：`GET /api/review/entities/{entity_id}/candidates`

返回该实体的候选 entity_dict 列表，供审核页展示"可能是同一实体"的候选项。

```python
@router.get("/entities/{entity_id}/candidates")
def get_entity_candidates(entity_id: int, top_k: int = Query(10)):
    # 1. 查 extracted_entity 拿 entity_name/type/data
    # 2. 调 find_candidate_dicts_for_review
    # 3. 返回候选列表（附 is_verified 标记，前端区分"已审核"和"自动创建"）
```

---

## 数据流示意

```
Worker A: 文档A ("阿司匹林")        Worker B: 文档B ("阿司匹林")
─────────────────────────────────  ─────────────────────────────────
LLM 抽取 (并行)                    LLM 抽取 (并行)
  │                                  │
  ▼                                  ▼
┌─ 获取锁 ──────────────────────┐  等待锁...
│ 消歧: 查 entity_dict           │
│   → 无匹配 (new_entity)        │
│ insert Phase 2:                │
│   创建 entity_dict              │
│     is_verified=False           │
│   写 aliases                    │
│   插入 extracted_entities       │
│     dict_id → 新 entity_dict    │
└─ 释放锁 ──────────────────────┘
                                     │
                                    ┌─ 获取锁 ──────────────────────┐
                                    │ 消歧: 查 entity_dict           │
                                    │   → Step2 alias 命中"阿司匹林" │
                                    │   → auto_matched               │
                                    │ insert Phase 1:                │
                                    │   已审核实体? → 否(is_verified │
                                    │   =False, review=pending)      │
                                    │   同名 pending? Phase 2 步2    │
                                    │   → merge 到已有行             │
                                    └─ 释放锁 ──────────────────────┘

审核阶段（用户操作）:
  - 审核页看到"阿司匹林"的 pending extracted_entity
  - 调用 GET /candidates → 返回 auto-created entity_dict + 其他候选
  - 审核通过 → approve_entity_with_alias_writeback
    → entity_dict.is_verified = True
    → weknora 同步
```

---

## 不变文件

- `db/models.py` — 字段已齐全（is_verified、is_deleted、canonical_source）
- `disambiguation/engine.py` — 消歧逻辑不变，它本来就查 entity_aliases
- `config/sites/*.py` — 不受影响
- `core_engine/docx_parser.py` / `content_cleaner.py` — 不受影响

## 边界情况

| 场景 | 处理 |
|------|------|
| 同一文档内两次出现"阿司匹林" | batch_dict_cache 去重，只建一个 entity_dict |
| 两个 Worker 同时 new_entity 同一实体 | 锁保证串行，先执行的建 entity_dict，后执行的消歧命中 |
| auto-created entity_dict 被推送到 WeKnora | `get_entity_dict_for_weknora` 过滤 is_verified=True |
| 用户审核拒绝 auto-created 实体 | reject_entity 不改 entity_dict（仍保留，供后续消歧匹配） |
| 用户想删除某个 entity_dict | `soft_delete_entity_dict` 检查 canonical_source != "primekg" |
| PrimeKG 别名被误删 | `soft_delete_entity_alias` 检查父 dict 来源 + is_canonical 保护 |
| is_verified=False 的 entity_dict 没有 embedding | `add_aliases_no_embedding` 只写行，后续批量补 embedding |

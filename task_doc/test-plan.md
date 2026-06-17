# 测试开发计划

## 原则

- **mock 驱动，不连数据库**：所有单元测试用 `mock_session`，验证逻辑而非连通性
- **测改过的东西**：优先覆盖近期改动最大的模块（别名写入、实体生命周期、消歧）
- **一个文件对一个模块**：`tests/db/test_repository.py` 对应 `db/repository.py`

## 目录结构

```
tests/
├── conftest.py                  # mock_session, mock_engine, mock_get_session
├── db/
│   ├── test_repository.py       # 别名写入 + 实体生命周期
│   └── test_models.py           # ORM 模型不变量（name 不可变等）
├── api/
│   ├── test_review.py           # hide_primekg, 审批过滤逻辑
│   ├── test_extraction.py       # 入队, 超时
│   └── test_weknora.py          # 图谱同步过滤
├── disambiguation/
│   ├── test_normalizer.py       # 名称标准化规则
│   └── test_engine.py           # 候选排序, 阈值判定
└── flows/
    └── test_extraction_worker.py # 抽取全流程端到端（集成测试）
```

## 实施阶段

### Phase 0: 基础设施（已完成）

- [x] `pytest.ini` + 依赖
- [x] `tests/conftest.py` 共享 fixtures

### Phase 1: P0 — 数据层（priority: 最高）

**文件**：`tests/db/test_repository.py`

**范围**：`db/repository.py` 中近期改过的核心函数

| # | 测试对象 | 用例 | 验证点 |
|---|---------|------|--------|
| 1 | `_write_aliases_with_embedding` | 同 name + 同 entity_type + 不同 dict_id | 跳过插入，不产生重复 |
| 2 | `_write_aliases_with_embedding` | 同 name + 不同 entity_type | 允许写入 |
| 3 | `_write_aliases_with_embedding` | 制剂后缀实体（氯雷他定片）→ 子串"氯雷他定" | 别名被跳过 |
| 4 | `_write_aliases_with_embedding` | 非制剂实体 → 子串别名 | 正常写入 |
| 5 | `_write_aliases_with_embedding` | embedding 生成抛异常 | 不崩溃，alias 仍写入 |
| 6 | EntityAlias 构造函数 | entity_type 参数 | 非空，写入正确值 |
| 7 | 不变量 | entity_aliases.name 创建后 | 不被修改 |
| 8 | 不变量 | entity_dict.canonical_data.name | 不被修改 |

| # | 测试对象 | 用例 | 验证点 |
|---|---------|------|--------|
| 9 | `_do_insert_extraction_result` | 第2次命中同一实体 | review_status → auto_approved |
| 10 | `approve_entity` | 人工审批 | `_sync_canonical_to_extracted` 被调用 |
| 11 | `resolve_merge` | 冲突裁决 | `_sync_canonical_to_extracted` 被调用 |
| 12 | `_sync_canonical_to_extracted` | pending 实体 | entity_data 不被改动 |
| 13 | 关系去重 | 同 (from, to, type, doc_uuid) | 不重复插入 |

### Phase 2: P1 — API 路由

**文件**：`tests/api/test_review.py`, `tests/api/test_weknora.py`

| # | 测试对象 | 用例 | 验证点 |
|---|---------|------|--------|
| 1 | `list_entities` | hide_primekg=True | primekg 来源不出现在结果 |
| 2 | `list_entities` | hide_primekg=False | primekg 来源出现 |
| 3 | `list_entities` | entity_type 过滤 | 只返回指定类型 |
| 4 | `list_entities` | review_status 过滤 | 只返回指定状态 |
| 5 | `approve_entity` | 审批成功 | 返回正确 code, review_status 变更 |
| 6 | `get_entities_for_graph` | 图谱同步 | 只返回 approved/auto_approved |

### Phase 3: P2 — 消歧引擎

**文件**：`tests/disambiguation/test_normalizer.py`, `tests/disambiguation/test_engine.py`

| # | 测试对象 | 用例 | 验证点 |
|---|---------|------|--------|
| 1 | normalizer | 去空白/标点 | 标准化后一致 |
| 2 | normalizer | 大小写 | 不区分大小写匹配 |
| 3 | normalizer | 中英文虚实名 | 中文名 vs 英文名区分 |
| 4 | engine | 精确匹配 | 同名字段 → identity match |
| 5 | engine | 语义相近 + 未 approve | 返回合并建议 |
| 6 | engine | 语义相近 + 已 approve | 不做合并 |
| 7 | engine | 完全新实体 | 创建新 dict |

### Phase 4: P3 — 集成测试

**文件**：`tests/flows/test_extraction_worker.py`

| # | 测试对象 | 用例 | 验证点 |
|---|---------|------|--------|
| 1 | extraction_worker | 抽取全流程 | 入队 → LLM调用 → 入库（mock LLM） |
| 2 | extraction_worker | 超时处理 | 不崩溃，正确标记 |

## 运行命令

```bash
# 全部单元测试
pytest tests/ -m unit -v

# P0 数据层
pytest tests/db/ -v

# 覆盖率报告
pytest tests/ --cov=. --cov-report=term-missing

# 排除 WeKnora（第三方）
pytest tests/ --cov=. --cov-report=term-missing --ignore=WeKnora-main
```

## 不测的东西

- 数据库连通性（环境问题不是代码问题）
- LLM 调用本身（外部依赖，集成测试才需要）
- 前端组件（缺 Playwright 基础设施）
- 爬虫网络连通（不属于单元测试范围）
- WeKnora-main（第三方代码）

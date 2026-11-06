# 0003 整合线的交接清单与未完成项

- 状态：**待修复 / 待团队评审，功能边界如下；未被接受**。`integration/rebuild-2026-11` 不进 `main`、不部署，
  也不替代团队的主线（`feat/server_fix`）；进不进主线、哪些功能要在新链路上重做，由团队和负责人决定。
- 日期：2026-11-06
- 相关：合并的逐处处理见 [0001](0001-merge-server-fix.md)，处理器版本来历见 [0002](0002-processor-versions.md)。

## 一、团队功能在整合线上的实际状态

不能把这条线说成“保留了团队的全部功能”。按是否真的会运行，分三类：

### 在运行（新链路上有调用方，测试覆盖）

- 身份确认绑定固定原文：`db/identity.py`、`api/review_auth.py`、对象证据、自动发表图输入里的字典审核上下文和合并日志、
  `flows.graph_publish.bind_document_text`。
- 隔离模拟补采：`flows/history_fetch.py` 和 `FetchRequest.is_demo` / `demo_instance_id`。
- 来源材料性质 `document_evidence`（不参与版本身份，合并前首次见到的版本没有这一项，见 0001）。
- GDELT 新闻索引（来源目录里的插件，经统一读取闸门；不进持续监看）。
- 公开读取总开关 `DATAFABRIC_HISTORY_FETCH_ENABLED`。
- ClinicalTrials/FDA 的规则抽取：**只对 documents 库存的主体**（正文是来源接口原件）。
- 规范化规则补全（`disambiguation/`）、Europe PMC count_drift 修复。

### 保留但没有接入（代码和单元测试在，生产路径上没有调用方，不会运行）

**药名与标识符对齐库**：它唯一的调用方是旧 ETL 的 `core_engine/transform_pipeline.py` /
`core_engine/storage_writer.py`，旧 ETL 已按重构线的设计删除，所以**对齐从不触发**，`doc_authority_resolution` 表不会有新行。
没有生产调用方的函数（按名字列出）：

| 模块 | 只有定义、没有生产调用方的函数 |
|---|---|
| `core_engine/authority_resolver.py` | `resolve_identifier_multi`、`resolve_conflict`、`majority_vote`、`evidence_weighted`、`source_hierarchy`、`structural_criterion`、`platt_scale`、`platt_predict` |
| `core_engine/authority_client.py` | `resolve_identifier`（只被同一个库里的 `authority_resolver` 调用） |
| `core_engine/authority_store.py` | `save_authority_resolutions`、`load_authority_resolutions`、`summarize`、`authority_dir` |
| `biz_rules/drug_name_normalizer.py` | `generate_candidates`、`is_placeholder`、`pick_primary` |
| `db/repository.py` | `upsert_authority_resolutions`、`_collect_authority_rows`、`get_authority_resolutions` |

相关的表和配置：`doc_authority_resolution`（V033/V034）、`docker-compose.authority.yml`。
这一轮**不接入**（先清理，不做业务实验）。接到哪里由团队按论文口径定，新链路上的位置是加工层：按固定版本的清洗正文和投影字段
做一个带 `PROCESSOR_VERSION` 的派生步骤，结果进派生表。

### 退回到别的路径

- **History 来源的规则抽取 → 模型抽取。** 新采集的 ClinicalTrials/FDA 记录只在 History 里，作为抽取主体时交给抽取的是清洗正文，
  不是团队规则映射需要的接口原件，所以走模型抽取（`extract_single_document(native_allowed=False)`）。团队“不再让模型重抄
  编号”的目标在这类记录上**没有达到**。要做到，需要规则映射读固定版本的原件、证据定位指向原件的 JSON 位置，并和主体图发表的
  定位方式对上。

### 没有带过来（随旧链路删除）

旧 ETL 的清洗与质检改进、`/api/weknora/sync-batch` 停用提示、ETL 入口兼容，以及只测这些模块的测试。清单见 0001“没有带过来的改动”。

## 二、迁移编号（已处理）

迁移器按（版本号, 描述, 校验和）识别已执行的迁移，查的是由仓库历史生成的 `migration/known_migrations.py`；团队编号的
V020/V021/V022/V023 被认作今天的 V033/V034/V020/V035，原记录不删不改；认不出的组合直接报错、不写入任何内容。部署前可以用
`python -m migration.migrate --list` 只读查看某个库会被怎样识别（这一步只读，认不出时同样报错并列出原因）。

需要注意：**早期主线用过、后来被替换的迁移**（如 V010 `add composite indexes`、V012 `add content hash`）在
`known_migrations.py` 里标为 `superseded`，带这类记录的库会被拒绝，需要人判断这个库的真实结构后再处理。现役库是否带有这类
记录，这一轮没有查看（不访问现役库）。

## 三、仍未完成的清理

- 目录 api/biz_rules/collection/contracts/db/flows/processing/migration/ops 里还有 31 个超过 100 行的函数（`_publish`、
  `_project_record` 已拆）；合并带进来的 `native_graph.validate_field_bindings` cc 98。
- 测试代码里还有约 45 处评审编号，`tests/integration_codex_*.py` 两个文件名没改。
- `ops/check_clean_install.sh` 没有端到端跑过（要从软件源下载依赖）；只在现有环境里跑过 `ops/check_imports.py`。
- Fabric 没有 ruff 配置；默认规则下存量告警很多。
- index、clean_stage 的统计查询里还有几处直接写的租约判断（不是队列实现）。

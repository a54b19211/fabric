# 0005 来源目录由声明生成，能力由 Fabric 计算，legacy 退出新选择

- 状态：已实现（Fabric 一侧），评审通过并合入 `integration/rebuild-2026-11`（2026-11-07；原分支 `r2/fabric-b1`）；Radar 一侧见 BioRadar 的 ADR 0009（已合入）。设计：`r2-fabric-source-catalog-legacy.md` 及 r219 修订。
- 日期：2026-11-07

## 决定

- 来源的许可和法律事实只在 `biz_rules/source_licenses.py` 的 `LicenseSpec`（采集来源、派生来源两组）；退役来源在
  `biz_rules/retired_sources.py`。`SOURCE_CATALOG` 是生成视图，导入时与采集目录交叉校验（每个可采集来源恰有一份许可、
  没有来源出现在两组、采集来源不是图来源）。描述文字以代码为准；库里 `history_sources` 行只存管理员的决定
  （`read_enabled`、依赖、`access`、`published_seq`），其 `catalog` jsonb 是落库时的快照，迁移不再改写它。
- `GET /api/history/sources` 为每个来源给 `capabilities`：`readable`（库里对该行的当前判断，不从“可选”推出）、
  `read / list / search / select / fetch(modes) / graph / watch`，以及附件、清洗、登记差分、索引目标、回放模式。
  能力 = 许可状态 ∧ 已注册实现；受控归档和 local_files 保留阅读、检索、选择。`read_modes` 由插件注册并可按来源收窄。
- 两个摘要：`definition_sha256`（只含静态声明）与 `receipt_sha256`（含许可状态和进度）。能力快照只用于计划和审计，
  不授权任何后续动作；每次发送、重试、缓存命中、报告渲染前仍现查权限，按版本读取仍验证具体身份。
- legacy_documents：不在目录、选择、索引目标、默认值里；`DATAFABRIC_LEGACY_WRITES` 开关已删除（写入一律 410）；
  检索回执列 `excluded_sources`；索引目标配置了退役来源启动即拒绝（`index_target_retired_source`）；
  `publication_status` 的 `source_id` 必填；新图包不能引用退役来源（`graph_evidence_retired_source`），旧图包照常读；
  旧编号经别名读取，并重查原站点和真实来源的当前权限。旧数据、审计、`legacy_aliases` 不动，不倒填任何可见时间。
- 退役是只追加的生命周期事件（`history_source_lifecycle`，V036）：决定（2026-11-06，精度“日”）与生效时间（部署时由数据库时钟写入）
  分开，生效时间不取决定日期。回滚是追加 `retirement_withdrawn`。
- 部署窗口配置 `DATAFABRIC_RETIRED_IN_SOURCES`：只影响退役来源是否同时出现在 `sources` 数组，不影响任何能力。

## 保留的旧代码

旧写入函数不删除（RULES-r2：不删兼容实现），只是生产入口不可达；`db.history.legacy_retired()` 恒为真，旧写入契约测试通过
`tests/legacy_bridge.py` 显式替换它。删除另议。

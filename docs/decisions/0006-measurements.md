# 0006 客观测量归加工层：键、欠账、核验、事件与两种回放

- 状态：已实现（Fabric 一侧的切片 S1：WHO DON 病例数），评审通过并合入 `integration/rebuild-2026-11`（2026-11-07；原分支 `r2/fabric-s1`）；Radar 一侧（`series_points`、
  `measurement_inbox`、`series_sync`、旧测量表冻结为只读）见 BioRadar 的 ADR 0010（已合入）；planner 可选字段仍是后续事项。
- 设计：`devtools/design/r2-measurement-migration.md`、其 r219 修订 `r2-measurement-migration-r2.md`，
  Codex r219 B3 与 r222 的实施约束。日期 2026-11-07。

## 决定

- **规则搬家不改规则**：`processing/measure_rules.py` 是 BioRadar measure.v10（b452279 的 `measure_checks.py`、
  `continuation.py`、`material.py`）的窗口切分、上下文、引文定位、字面量定位与严格请求构建；只把“引用来源”换成本处理器
  自己发给模型的窗口和上下文。`tests/test_measure_rules.py` 用 BioRadar 自己的函数生成的 golden 逐字段比对。唯一的
  有意差别：第 0 个阅读窗口也在行或句子的边界结束（BioRadar 在 1500 字处硬切；D1 的 421 个 WHO DON 版本里有 4 个把数字
  切成两半），所以超过一个窗口的长文本不再比对窗口范围，其余字段照旧比对。
- **键**：run = 固定原件（来源、记录、版本、raw sha256）+ 清洗产物（清洗器版本、正文哈希）+ 处理器（版本 + 模型配置
  canonical 哈希：kind、model、endpoint_id、thinking、temperature、max_tokens；不含 URL 和密钥）；读数 id = 完整候选
  （送 BioCheck 的严格请求项去掉调用方 id + passages + 三段键）；对象 id = 读数 + 核验器版本。同一跨度两种解释是两个读数。
- **模型调用**经共享包 `zk-llm-transport` 0.1.1：每次 HTTP 前 `before_attempt` 现查租约和来源许可并在自己的已提交事务里
  登记请求（撤权则一个请求也不发），之后 `after_attempt` 一次性结算并存原始响应（触发器只允许结算一次）。
- **欠账**：窗口失败退避重试，3 次后 `failed_final`，run `partial`，覆盖回执列出；只有显式 `retry_failed()` 重新排队。
  预算用完（本轮额度或 429/503）是 `budget_spent`，不计失败次数。撤权 → `source_unreadable`、run `paused_unreadable`，
  恢复后 `resume()`。
- **核验按产物选契约**：`CHECKS_BY_PRODUCER`：编号、画像注释 → `/v1`（v1 契约、checker v3）；测量 → `/v3`（v3 契约、
  checker v16）。测量一读数一请求，复用 `derived_extraction_check_tasks`；全部批次完成、租约和许可仍在时才发布对象。
  未被采纳的晚到回答（核验期间撤权、租约已失）把回答条目存进 unchecked 行作审计；已被替代的 run 晚到的回答产生的对象
  立即被替代，从不成为当前。
- **对象只追加，状态来自事件**：`derived_measurement_events` 记 created / superseded_by_processor / superseded_by_producer /
  rechecked_by_checker / corrected / retracted / correction_withdrawn，各带三个时间（材料可见、结果产生、状态生效）；
  feed 序号在同一行锁下取，顺序即提交顺序。
- **两种回放分开**：`operational_replay` 只折叠截止时已产生且材料已可见的事件，只给截止时已产生的对象；
  `public_history_reconstruction` 用今天的处理器和核验器，材料（含更正）按其历史证明时间，回执写 `reconstruction: true`
  并列出所用版本。`current` 下旧核验器的对象在重核前不是当前。
- **更正**：按来源和 published_seq 的有界扫描（游标）把 `document_relations` 里有目标版本的 corrects / retracts 记成链接；
  同一更正记录的后续版本不再声明则记撤回。目标版本的对象按链接追加事件——更正先到或测量先到都一样。
- **身份**只来自已确认的图谱身份（`graph_tools.confirmed_identity`），且该节点自己的证据就是这条测量的对象提及（同版本、
  同清洗产物、跨度含对象字面）；否则是记录内的字面键 `literal:<来源>|<记录>|<规范化字面>`，同名不合并（Q7）。
- **能力门控**：`processing.registry.MEASURE_SOURCES`（S1 只有 who_don）且来源记录种类为原件；`/sources` 的
  `capabilities.measurements` 只供计划，每次读取仍现查许可（含事件起因来源和图谱来源）。

## 与设计稿的出入

- `derived_measure_runs.state` 没有 `planned`：run 建立时窗口一起建立，直接是 `reading`。
- `/extraction-checks` 回执对编号、画像两个产物逐字不变（S1 固定点），测量的核验结果经 `/measurements`、
  `/measure-coverage` 读，不并入该回执。
- 只带记录编号、不带目标版本的更正关系记为对目标记录的声明（V040）：未撤回、未落到版本之前，该记录的所有对象
  `status.state=unknown`（`correction_target_version_unknown`）、不是当前、默认列表排除并计数、覆盖回执列出；
  `resolve_correction()` 追加落版本的记录后照常产生 corrected / retracted 事件；`rescan_corrections()` 显式重扫。
- 回放里哪个核验器在截止时生效没有记录：operational_replay 下，核验器不是当前配置的那个的对象一律
  `current=false`、`not_current_reason=checker_history_unknown`，从不显示为当前的 ok。

## 后续（未实现）

- **核验器启用历史**：新迁移里一张只追加的表（每个 producer 的核验器启用，写入时用数据库时钟，绝不倒填，不从任务表推断）；回放取截止时生效的那一条：对象核验器不同且截止时看不到重核 → `pending_recheck_by:<checker>`；截止时还没有任何启用记录 → `policy_history_missing`。一次启用覆盖该 producer 的全部对象，不为每个对象写事件。有了它，`checker_history_unknown` 才能换成确定的答案。

## 重读替代的匹配

处理器或清洗器升级后，新旧对象按**去掉位置和 producer 的完整候选**匹配（引文、各字段状态与终值、口径模式、类型、限定、情态、指标族、每个维度），只在一一对应时替代；同一候选在新读里出现多次 → 重定位记 owed（`ambiguous_candidate`），不替代。只差单位、维度或类型的两个候选从不互相替代。

## 两条候选路线与计划范围入口（任务书 01 §2.7）

- **一个入口，两条候选路线**：模型路线不变；规则路线把 `who.explicit-count.v1`（`biz_rules/who_facts.py`）的明确计数从清洗正文
  （按产物自带的段落定位 Summary、Overview，不读标题和表格）写成模型会写的 measure（`processing/measure_counts.py`），走同一套
  严格请求构建、口径、身份和 BioCheck v3/v16 核验，不调模型。没写明累计或新增的计数不猜，记原因（`measure_kind_other_is_not_checkable`）。
  投影里原有的正则结果不动。“正则精度高”仍是待验证的假设，不是结论。
- **路线写在生产者标识里**（`…v2+rules.who.explicit-count.v1`），规则路线的运行没有模型配置摘要；一条路线只替代自己的旧运行，
  两路读数不互相替代、不按路线优先级覆盖。
- **一致与冲突**（`processing/measure_agreement.py`，读时算，不另存）：值落在同一位置的两个读数，比对象、指标、单位、时期、
  地区或人群、累计或新增、疑似或确诊、条件、限定词 9 项，逐项按写法比（小写、空白折叠，不猜同义词）；9 项都一致才 `agree`，
  否则 `conflict`，两条都留。“条件”取情态和读数写出的日期口径，“地区或人群”取范围和读数写出的人群，“疑似或确诊”从指标的词里读。
  两路读同一原文，`independent_evidence` 恒为 `false`。
- **计划范围入口**：`plan_scope()`/`Scope` 只计划清单里的版本，`process_once(scope=…)` 只读清单里的版本；清单没点名的阅读窗口存为
  `skipped`（`out_of_scope`），不是欠账；清单摘要和所选窗口写在运行的 `selection` 上（V041）。常驻队列不计划有界回填新建的版本
  （`db.history.background_version_ids`）。清单要求不能满足的版本（还没有清洗正文、窗口号不存在……）在回执里逐项写原因。
  同一产物同一路线已有运行时清单不改它：窗口范围一旦定下不会被后来的清单放大。

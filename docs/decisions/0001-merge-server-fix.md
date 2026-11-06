# 0001 合并 feat/server_fix 到重构线

- 状态：待团队评审（LazyLazyFolivora、xvziwen、WWTang）；合并前不进 `main`、不部署
- 日期：2026-11-06
- 分支：`integration/rebuild-2026-11`，底为 `feat/t26-clean-v12`（c9ed485，已含 t29/t30），合入 `feat/server_fix`（d316aad）
- `dev`（dccbc2a）已全部在 `feat/server_fix` 里（49c923f 合入），没有单独再合

## 背景

两条线从 `82bad7b`（10-30）分开。重构线删掉了第二条采集链路（合并 M1–M6：旧 ETL、文档重爬/上传、WeKnora 文档
同步、手工 capture/publish-legacy 命令、第二份清洗器），改由采集框架（`collection/`）、History 固定版本、加工层
（`processing/`）和 `db/index_publication` 各管一件事。团队线在同一段时间里加了：身份确认绑定固定原文、隔离模拟补采、
来源材料性质（`document_evidence`）、GDELT 新闻索引、公开读取总开关、药名与标识符对齐（A0–A1）、规范化规则库补全，
以及 11-03 的 Europe PMC count_drift 修复。

git 报出 19 个文件内容冲突（diff3 下 36 处）和 14 个“一边删、一边改”的文件。另外还有 git 不报、但会直接出错的语义冲突
（迁移编号、被删模块的新调用方）。下面逐条列出。

## 总原则

1. 重构线已经删掉的链路不恢复（“一条路”是验收过的设计，也是审计的硬指标）。团队在这些被删文件里的改动，凡是新链路
   上有对应位置的，把意图移过去；没有对应位置的，不带过来，在“没有带过来的改动”一节逐条列出，由团队决定要不要在
   新链路上重做。
2. 团队新增、能独立成立的模块都保留（含测试），即使暂时没有调用方（药名与标识符对齐库），不替团队删掉研究成果。
3. 重构线已上线或已验收的行为不变：版本身份、读取闸门与预算、监看来源集合、迁移编号、索引请求体都保持原样；
   与之冲突的团队改动按这些接口改写，改写处都有测试。

## 内容冲突（逐处）

| 文件 | 处 | 重构线（ours）的意思 | 团队（theirs）的意思 | 处理 |
|---|---|---|---|---|
| `biz_rules/history_sources.py` | 1 | 加 `import math` | 加 `import os`（读取开关） | 两行都留 |
| 同上 | 2 | `project_record` 先拆 v3 6.3 迁入封装再投影 | `project_record` 投影后附 `document_evidence` | 合成一个入口：拆封装 → 投影 → 附 `document_evidence`（按拆封装后的原件算）→ 写迁入块 |
| 同上 | 3 | 加 `_read_who` / `_read_sec` 插件函数 | 加 `require_public_fetch_enabled()` | 都留 |
| 同上 | 4 | `read_public` 改为按来源目录找插件、整次读取过闸计量 | `read_public` 里加模拟补采禁止外采、读取总开关、GDELT 分支 | 保留插件分派；开头加团队的两项检查；GDELT 注册为插件 `gdelt_doc`，并在 `collection/catalog.py` 登记 |
| 同上 | 5–7 | count_drift 修复（live 热修已带进重构线） | 同一修复（团队 a7e0c1f/d316aad） | 两边逻辑相同，取 ours 的写法；`db/history.finish_fetch` 写覆盖说明的那段由自动合并带入，只有一份 |
| `biz_rules/history_models.py` | 1 | `HistoryRecord.migration` | `HistoryRecord.document_evidence` | 都留 |
| 同上 | 2 | `FetchRequest.source_id` 改成任意串，由目录校验 | 字面量列表加 `gdelt_doc`、`external_archive`，加 `is_demo`、`demo_instance_id` | 用 ours 的串类型 + 团队两个字段 |
| 同上 | 3 | 校验来源在 `FETCHABLE` | 模拟补采的四项校验 | 先做团队的校验（保留原报错文字），再查目录；`external_archive` 只许模拟补采 |
| `biz_rules/cleaners/__init__.py` | 1 | `full_to_half` 改用编号抽取的同一实现 | 加 `alnum_only` 档，不转中文标点 | 默认档用 ours 的共用实现，`alnum_only=True` 用团队的实现 |
| `core_engine/api_router.py` | 1 | 删掉旧血缘记录函数 | 改 `_get_nested` 签名以支持候选路径 | 不恢复血缘函数；签名用团队的（候选链的函数体由自动合并带入） |
| 同上 | 2 | `to_result` 改为返回 (结果, 原记录)，去掉 `timestamp`/血缘 | 与基线相同（只有缩进差异） | ours |
| `api/routers/weknora.py` | 1–2 | 删掉文档同步接口（M4） | 把 `/sync-batch` 标为停用 | ours（整组接口已删，团队“停用批量同步”的意图已满足） |
| `db/history.py` | 1 | 覆盖说明里记附件读取情况 | 覆盖说明里记模拟补采身份 | 都留 |
| `db/index_publication.py` | 1 | `_manifest` 新增产物、元数据、分片参数及上面的辅助函数 | `_manifest` 开头校验目标与原件来源空间一致 | ours 全部 + 团队的校验 |
| 同上 | 2 | 清单 `is_demo` 取目标 | 清单 `is_demo` 取原件 | ours（上一处校验保证两者相等） |
| 同上 | 3 | `_current_state` 只返回期望状态；从未发出直接停用；退役先等替代 | 校验清单 `is_demo` 与目标一致，并把旧清单补上标记 | ours + 团队的一致性校验。“补标记”没有带：ours 发给桥接的请求体本来就从目标配置取 `is_demo`（`_bridge_body`） |
| `db/models.py` | 1 | import 整理成一行 | import 加 `UniqueConstraint` | ours 的写法 + `UniqueConstraint` |
| `db/publication.py` | 1 | `build_legacy_bundle` 改名 `build_document_bundle` | 加 `bind_legacy_text`（把内联的定位代码抽成函数） | 两者都用；新函数按 M6 的命名改成 `bind_document_text` |
| `db/repository.py` | 1 | 导入 `ReviewLog` | 导入 `DocAuthorityResolution` | 都留 |
| 同上 | 2 | 删掉 `reset_document_for_recrawl`（重爬接口已删） | 原样保留它，后面加证据/外部编号查询 | 不恢复重爬函数；团队新加的 `db.identity` 导出和 `lookup_external_id_candidates` 保留；节标题改为“来源证据与外部编号查询” |
| `flows/graph_publish.py` | 1 | 删掉 `capture_legacy`（M5：手工命令读旧 HTTP 接口） | `capture_legacy` 带上证据范围、字典审核上下文、合并日志 | ours。同样的改动团队也做在了自动发表路径（`db/publication._graph_input`），已由自动合并带入 |
| 同上 | 2 | `build_legacy_bundle` 改名 | 新增 `_auditor`、`_item_evidence` | 团队的两个函数 + ours 的函数名 |
| 同上 | 3 | 删掉 `publish_legacy`（M5） | `publish_legacy` 里调用新函数 `bind_legacy_text` | 不恢复 `publish_legacy`；`bind_legacy_text` 保留并改名 `bind_document_text` |
| `migration/index_publication.py` | 1 | 依次执行 V020/V024/V027/V028/V031 | 读改名后的 `V022__weknora_index.sql` | ours（见下文“迁移编号”） |
| `tests/integration_history.py` | 1 | 夹具改用测试内的 `_publish_fixture_graph` | 夹具先冻结原件、补审核上下文，再 `publish_legacy` | 团队的准备步骤 + ours 的夹具函数；夹具函数照团队对 `publish_legacy` 的改动也调用 `bind_document_text` |
| 同上 | 2 | 新增数据日期、附件、迁入等用例 | 新增全文更正、同键补采、GDELT 用例 | 都留 |
| `tests/integration_publication.py` | 1 | 新增对账用例 | 新增第二份来源佐证用例 | 都留（迁移文件名改为 V035） |
| `tests/test_history.py` | 1 | count_drift 等用例 | 同样的 count_drift 用例 | ours（团队两条用例 ours 里已有，内容相同） |
| `tests/test_reviewed_graph.py` | 1 | 改名 | 旧 `is_verified` 只能是提案的新断言 | 团队的断言 + 新函数名 |
| `ARCHITECTURE.md` | 1–4 | 在旧长文上改了几处 | 整篇改写为 99 行的新版 | 用团队的新版；“改代码到哪里找”按合并后的真实目录重写（团队版里的 `core_engine/acquisition/` 等子包已被团队自己在 49c923f 撤回，`flows/etl.py`/`crawl_flow.py` 已删） |
| `README.md` | 1–2 | 目录结构按新框架改写 | 目录结构里写 `main_etl.py` 兼容入口 | ours；“版本使用”一段改为说明本合并分支 |
| `task_doc/数据采集与图谱修改报告.md` | 1 | 标注“已过时” | 标注“按日期保留记录” | 合成一句：按日期保留、不改写，并指明哪些入口已删除 |

## 一边删、一边改的文件

| 文件 | 团队改了什么 | 处理 |
|---|---|---|
| `main_etl.py` | 改为转到 `flows/etl.py` 的兼容入口 | 删除（M1）；`flows/etl.py`（团队新增）一并不要 |
| `flows/crawl_flow.py` | 抽出 `classify_store_result` | 删除（M1） |
| `core_engine/transform_pipeline.py` | 药名拆分、候选回退对齐、标识符格式校验 | 删除（M1）；对齐逻辑见“没有带过来的改动”1 |
| `core_engine/storage_writer.py` | 站点字段补齐、对齐结果旁路文件与入库 | 删除（M1）；同上 |
| `core_engine/content_cleaner.py` | 标题清洗、字段值只转全角字母数字、对所有站点执行通用清洗 | 删除（M3：`processing/clean_text` 是唯一清洗器）；`alnum_only` 档保留在 `biz_rules.cleaners.full_to_half` |
| `biz_rules/document_validator.py` | 新增 4 个文档类型枚举 | 删除（M1）；新链路的文档类型在 `processing/profile` 和来源目录 |
| `biz_rules/source_mapper.py` | 补登记 5 个一手来源域名 | 删除（M1）；来源可信度按来源目录，不按域名表 |
| `biz_rules/extractors/pubmed/pubmed_fetcher.py` | `normalize_pmcid`，把标识符写进 structured_data | 删除（M1：框架原生读 PubMed）；见“没有带过来的改动”3 |
| `frontend/src/api/weknora.ts`、`frontend/src/views/WeknoraManager.vue` | 适配停用的批量同步 | 删除（M4） |
| `core_engine/authority_client.py` | ChEMBL 第二权威库、本地格式校验族 | **保留团队版本**：对齐库的一部分（见总原则 2） |
| `tests/test_authority_client.py` | 跟随上面 | 保留 |
| `tests/test_main_etl_results.py`、`tests/test_quality_checker.py` | 跟随旧 ETL | 随被测模块删除 |

## 语义冲突（git 不报）

1. **迁移编号。** V001–V032 已按重构线的编号用在 History/对象库上。团队把 `V020__weknora_index.sql` 改名为 V022，并新增
   `V020__doc_authority_resolution`、`V021__doc_authority_skipped`、`V023__identity_evidence`，与重构线的 V020–V023 撞号
   （迁移器按 `Vnnn` 记账，撞号的那一份会被当作已执行而跳过）。处理：重构线编号不动；团队三份改为
   `V033__doc_authority_resolution.sql`、`V034__doc_authority_skipped.sql`、`V035__identity_evidence.sql`，引用它们的测试同步改名。
   三份都是 `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`，重复执行无害。**已经按团队编号迁移过的库**（如果有）：
   `schema_migrations` 里的 V020–V023 记的是团队那几份，重构线的 V020–V023 会被跳过；需要人工核对后删掉这四条记录再跑
   一次迁移（重构线 V020–V023 也都是 `IF NOT EXISTS` 写法）。
2. **`document_evidence` 不进版本身份。** 团队把它放进投影，而投影整体参与 `version_id` 计算；直接合并会让合并前见过的
   每条记录在下次刷新时都多一个版本（现役约 60 万个版本），并带动重清洗、重画像、重建索引。它完全由原件和投影算出，
   不能区分两个版本，所以照存进新版本的 payload，但不参与版本身份（`db/history.DERIVED_PAYLOAD_FIELDS`）。代价：合并前
   首次见到的版本没有 `document_evidence`（读出为 `null`）。用例：
   `tests/integration_history.py::test_a_version_stored_before_document_evidence_still_matches_its_original`。
3. **附件版本不是 PubMed 文章。** 重构线把 PubMed 全文、站点附件存成 `role=attachment` 的 JSON 版本；团队的
   `document_evidence` 对所有 PubMed 版本按 XML 解析更正关系，遇到附件直接报错。处理：附件版本不附来源材料性质
   （看父记录），更正关系只从文章 XML 读。
4. **GDELT 接入新链路。** 读取经过统一闸门（`collection.transport`）：429 和超时按全局设置重试，等待超出截止时间就按
   预算停止，`stop_reason` 写停止原因，不算成功的空结果。团队的故障用例在“只发一次请求”（`TASK_RETRIES=0`）下原样
   通过，另加了重试后停止的用例。目录里登记为 `active`，增量类型记为 `none`：GDELT 有时间窗但没有续页，一页满了不知道
   窗口里还剩多少，不能做持续监看；监看来源集合仍是原来 4 个（`test_watchable_set_equals_previous_allowlist`）。
5. **规则抽取只读来源原件。** 团队把 ClinicalTrials/FDA 的抽取改为按原字段映射（`biz_rules/native_extraction.py`），
   它要在来源接口原件里定位字段；重构线把 history 固定版本也作为抽取主体，交给抽取的是该版本的清洗正文。两者相遇时
   规则映射找不到原字段，整条抽取失败。处理：documents 库存的主体照团队的规则映射；history 主体
   （`db/extraction_subjects.Subject.is_history`）继续走模型抽取（`extract_single_document(native_allowed=False)`）。
   要让 history 主体也按规则映射，需要让映射读固定版本的原件、证据定位指向原件的 JSON 位置，并和主体图发表的定位方式对上，
   见“没有带过来的改动”4。
6. **团队测试按旧接口写的地方**：`tests/test_index_demo.py` 按新的 `_manifest(target, record, artifact, meta)` 签名和回执
   里的 `artifact_key` 改写；`tests/test_publication.py` 去掉对不存在的 `history_publish.index_tick` 的替身；
   `tests/test_reviewed_graph.py` 里 `LegacyDocument` → `StoredDocument`；`tests/conftest.py` 去掉替 `storage_writer`
   打桩的全局夹具和只给已删质检用例用的信封工具；`tests/integration_demo_fetch.py` 清空 History 时加 `CASCADE`
   （派生表外键指向 `history_versions`）；`tests/integration_identity_review.py` 的对象库夹具补上 V026（主体列），
   冻结旧文档的用例打开切换期开关 `DATAFABRIC_LEGACY_WRITES=on`（v3 6.3 后旧桥默认只读），迁移编号断言改为合并后的编号；
   重构线的 `tests/integration_extraction_subjects.py` 里 `_disambiguate` 的替身接受团队新增的 `native_source` 参数，
   `tests/test_extraction_runtime_safety.py` 断言的调用参数加上 `native_allowed=True`。
   `integration_identity_review.py` 有 5 条、`integration_demo_fetch_runtime.py` 有 1 条在 `feat/server_fix` 自身上也不通过
   （迁移编号断言过期、发布夹具没跑身份迁移、运行时用例要求事先迁移好的 History 库）；合并后修正夹具，全部通过。

## 没有带过来的改动（请团队决定）

1. **药名与标识符对齐的“通路”。** 对齐库本身（`core_engine/authority_client.py`、`authority_resolver.py`、
   `authority_store.py`、`biz_rules/drug_name_normalizer.py`、`db.repository.upsert_authority_resolutions`、表
   `doc_authority_resolution`、`docker-compose.authority.yml`）和它们的单元测试都保留并通过；但调用它的是旧 ETL 的
   `transform_pipeline` / `storage_writer`，已删除，所以现在**没有任何代码会触发对齐**。新链路上自然的位置是加工层（按固定
   版本的清洗正文和投影字段做一个带 `PROCESSOR_VERSION` 的派生步骤，结果进派生表），需要团队按论文口径设计。随旧链路
   删除的测试：`test_drug_name_pipeline.py`、`test_identifier_alignment.py`、`test_identifier_pipeline.py`、
   `test_authority_resolution_table.py` 和 `test_authority_store.py` 里测写库双写的几条。
2. **旧 ETL 的清洗与质检改进**（`content_cleaner` 的标题清洗和字段值清洗、`document_validator` 的新枚举、`source_mapper`
   的来源域名、`test_station_fixtures.py` / `test_content_cleaner.py` 的站点样本）。新链路的对应位置是 `processing/clean_text`
   和 `processing/profile`；那里改规则要升处理器版本。
3. **PMCID 规范化**（`pubmed_fetcher.normalize_pmcid`）。团队修的是 esummary 返回值带键名前缀
   （`PMCpmc-id: PMC…;`）的问题。框架原生的 PubMed 投影不读 esummary，从 EFetch XML 的
   `ArticleId[@IdType='pmc']` 取 PMCID，全文路由（`collection/pubmed_fulltext.fulltext`）还按 `PMC\d+` 校验，所以新链路上
   没有这个问题，不需要移植。
4. **history 主体的规则抽取**（见“语义冲突”5）：新采集的 ClinicalTrials 记录只在 History 里，目前仍由模型抽取。
5. **ETL 入口兼容**（`test_engine_import_compatibility.py`、`main_etl.py` 转发）和 **`/api/weknora/sync-batch` 停用提示**
   （`test_weknora_batch_retirement.py`）：入口与接口已整体删除，相应用例随之删除。

## 验证

- 单元：合并前（c9ed485）1002 通过；合并后 1356 通过（多出的是团队新增用例），无跳过、无失败。
- 集成（自建一次性 PostgreSQL/Redis 容器）：合并前 245 通过；合并后 305 通过，无跳过、无失败。团队新增的集成用例
  （`integration_demo_fetch*.py`、`integration_identity_review.py`、`integration_history.py` 和
  `integration_publication.py` 里新增的几条）都在其中。

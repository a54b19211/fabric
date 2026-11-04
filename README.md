# ZK_Data Fabric — 数据基座 ETL

面向药学领域的数据采集与处理流水线，支持网页爬取、本地 PDF 批量解析、AI 结构化提取、质量门控与人工审核回灌。

## 版本使用

zktest 开发和运行统一使用 `dev`。截至业务提交 `4e7fb67`，本轮九个提交已归入 `dev`，包括任务保护、原文版本、连续发布、图谱接入与来源刷新。原 `main` 和其他学生分支保留，本轮未推送远端。联调工作目录没有独有的业务改动。正常修改按任务提交，不按版本或日期另开交付分支。

徐子雯组本阶段的开发安排见[数据采集工作说明](task_doc/数据采集工作说明.html)（下载后用浏览器打开）。其中列明数据用途、来源任务、现有代码位置、雷达交接、验收办法和研一任务拆分；本阶段任务以这份说明为准。下面的修改报告继续保留既有改动记录和技术说明。

徐子雯、唐蔚雯两组接手先读[数据采集与图谱修改报告](task_doc/数据采集与图谱修改报告.md)：改动范围、旧程序行为变化、具体任务、检查和回退集中在这一份。下文保留详细接口与运行说明。

---

## 环境要求

- Python **3.11.9**

---

## 快速开始

### 1. 创建虚拟环境

```bash
python -m venv .venv
```

激活虚拟环境：

- Windows（CMD）：
  ```cmd
  .venv\Scripts\activate.bat
  ```
- Windows（PowerShell）：
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
- macOS / Linux：
  ```bash
  source .venv/bin/activate
  ```

### 2. 安装依赖

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```



## 运行

数据链路只有一条：采集框架把原件写成只追加的 history 版本，加工层按版本产出派生结果，
正文经 `db/index_publication` 和桥接服务进 WeKnora。

| 层 | 入口 | 说明 |
|---|---|---|
| 来源目录 | `collection/catalog.py` | 每个来源一份声明（插件、限速组、增量键、是否实测）；调度、许可目录、加工层都读它 |
| 采集 | `POST /api/history/fetch`、`db/source_refresh.py` | 读取插件见 `biz_rules/history_sources.py` 的 `PLUGINS`；网站和站点接口统一走 `collection/site_crawler.py` |
| 附件 | `flows/process_attachments.py` | 附件是独立版本，正文由 `processing/attachment_parse.py`（MinerU）产出 |
| 清洗 | `flows/clean_stage.py` | `processing/clean_text.py`、`identifiers.py`、`profile.py`，分块租约并行 |
| 索引 | `db/index_publication.py` | 唯一的进 WeKnora 路径 |
| 发表 | `flows/history_publish.py` | 审核图发布与 documents 库存冻结 |

本地导入文件（原 `--mode local`）用 `POST /api/history/archives`；重抓页面用 `query=by_url` 的读取请求。

---

## 项目结构

```
├── collection/              # 采集框架：来源目录、站点插件、共享限速、增量窗口
├── processing/              # 加工：附件解析、清洗、编号、画像、登记差分
├── db/                      # history 存储、派生表、索引发布、documents 库（抽取/审核用）
├── flows/                   # 常驻任务入口
├── config/
│   ├── settings.py          # 全局配置（API Key、路径、阈值）
│   ├── crawler_settings.py  # 加载站点配置
│   └── sites/               # 每个站点一份配置（列表页、选择器、翻页、接口拉取函数）
├── core_engine/             # 抓取器、翻页驱动、MinerU 客户端、抽取与智能体
├── biz_rules/               # 站点提取器、history 模型与读取插件、图谱规则
└── contracts/               # 数据契约与业务验收样例
```

---

## 新增采集站点

1. 在 `config/sites/` 加一个站点配置（`SITE_KEY` + `CONFIG`）；
2. 在 `collection/catalog.py` 加一条 `CollectionSpec(plugin="site_crawler", verified=False, ...)`；
3. 加契约样例测试（`tests/test_site_crawler.py` 的写法），对原站实测后再把 `verified` 改为 True。

---

## 抽取服务的运行保护（2026-10-16）

本次修复基于 `9bb63b89`，提交为 `4e7ba15`，现已归入本地 `dev`。
服务器配置保留，不改数据库结构，不重跑历史资料。

| 修改位置 | 行为变化 |
|---|---|
| `api/main.py` | API 启动只注册已有监听器，不清队列、不重置任务 |
| `flows/extraction_worker.py` | 首次查询或暂停不再自动重发在途任务 |
| `core_engine/task_queue.py` | 读取队列或暂停状态失败时明确报错，不当成零任务或操作成功 |
| `api/routers/extraction.py` | 查询及暂停/恢复失败返回非成功状态；无法判断任务失活时，批量重置返回 409 |
| `flows/extraction_rq_job.py` | 暂停检查失败时停止抽取，尝试保存失败原因，并让 RQ 记录失败 |

原来的作业函数、UUID 参数和五小时超时保持不变。原 `/reset-stale` 批量重置
入口暂时返回 409，因为超过某个时间并不能证明任务已经失活。数据库不可写时，仍需
核对 RQ 与数据库状态；本次没有实现完整的任务恢复或自动重试。

### 检查方法

普通测试使用项目依赖，包含本次新增的 17 项运行保护检查：

```bash
python -m pytest
```

真实数据库/队列检查需要新建、用完可删除的本机 PostgreSQL 和 Redis。
数据库名和用户均为 `datafabric_runtime_test`，使用无业务数据的专用测试环境；
两个端口须指向这些测试服务。不要填写现役服务端口。

```bash
DATAFABRIC_TEST_DB_PORT=<测试数据库端口> \
DATAFABRIC_TEST_REDIS_PORT=<测试Redis端口> \
python -m pytest -s tests/integration_extraction_runtime.py
```

脚本要求数据库和 Redis 为空，然后创建合成材料，实际启动 RQ Worker，
两次启动并停止 API 进程，检查在途和排队任务保留、暂停/继续、失败写回及
Redis 不可连接的情况。专业抽取使用明确的测试替身，不调用真实模型。

### 回退方法

基线标记为 `baseline/zktest-20261016`，本次修复标记为
`release/zktest-task-safety-20261016`。可以在独立副本中撤销这一个修复提交：

```bash
git revert --no-commit 4e7ba15
```

**不要在仍运行着 `uvicorn --reload` 的目录直接执行回退。** 原版本启动会
清队列，首次查询状态还会重发在途任务。运行服务要回退时，先停止新任务受理，
完整停止 API 的 reload 父子进程和 Worker，核清 Redis 排队/在途任务及数据库
queued/processing 记录。确认没有未处理任务后，才能切回基线并按原方式启动。
无法确认时保留保护修复，先处理任务，不直接启动旧版本。

## 既有临床登记的有界研究读取

此能力只读取本库中标记为 `clinicaltrials`、未软删除的既有记录；不触发采集、
实体抽取、审核或下游同步，也不新增数据库表或队列。数据生产部分由徐子雯维护，
雷达怎样提出问题、派生追加查询和组织调查由雷达组维护。

### 按标题字面词组检索

在获准的测试机本地执行，例如：

```bash
curl --fail-with-body --get http://127.0.0.1:8100/api/research/documents/search \
  --data-urlencode 'terms=BCMA' --data-urlencode 'terms=myeloma' \
  --data-urlencode 'limit=6'
```

`terms` 必填，1 至 5 个词组，每个去除首尾空白后不超过 128 字符；所有词组都须在
同一标题中按不区分大小写的字面子串匹配。`%`、`_` 和反斜杠按字面处理，不是通配符。
`limit` 默认 6、最大 20。返回范围固定为 `title_literal_and_current_db`，
按内部 `id` 升序选取，额外读一条判断 `has_more`，不做总数统计或跨页读取。
`has_more=true` 说明本次截断；空结果只说明本库本次标题条件未命中，不代表领域中不存在。
接口不搜索正文、同义词或任意自然语言含义，不保证一组结果对应全来源覆盖。

### 读取选中记录的原始字段投影

将检索返回的 UUID 代入：

```bash
curl --fail-with-body http://127.0.0.1:8100/api/research/documents/<uuid>
```

返回 `source_url`、原样 `publish_date_raw`、采集/建库/更新时间及 `observed_at`。
这些时间分别保留，不把建库、采集或本次读取时间当首次公开时间。
ClinicalTrials 旧 `publish_date` 的语义有问题，仅原样保留，不用于推断试验开始时间。

`projection.version=clinicaltrials.protocol.v1` 只识别已知 Markdown“原始数据”附录中
唯一的 `protocolSection` JSON。`projection.text` 是原样 JSON 切片，
`locator.start/end` 是在原 `raw_content` 中的 Unicode 字符下标，
`document_sha256` 是完整 `raw_content` 的 UTF-8 哈希。
`facts` 提供注册号、原始标题/摘要、疾病、干预名称和类型、阶段、登记状态、停止原因，
以及首次公开、最后更新、开始和完成等来源日期结构；`field_paths` 给出原始 JSON 路径。
日期精度与 ACTUAL/ESTIMATED 原样保留。缺字段为 `null`，真实空数组保留为 `[]`。
不使用旧叙述里可能错误的“药物”和“开始日期”，不修改落盘内容，也不全库回填。

原始片段缺失、重复、JSON 损坏或字段不符合已知结构时，返回文档但明确
`projection.status=unavailable`、`reason`，不编造空事实。顶层 `hasResults` 不在
本投影内，缺少结果信息不能填成 `false`，也不能从 COMPLETED 推出已有公开结果。
TERMINATED、WITHDRAWN、SUSPENDED 是登记状态，不能自动认定疗效或安全失败。

### 预算、访问范围和检查

查询事务只读，每条数据库语句限时 3 秒；单篇原文超过 2 MiB 返回 413。
查询失败或超时返回 503，不伪装成成功空列表；来源不符、删除或不存在的记录返回 404。
来源标记筛选不是用户身份验证。本接口沿用现有测试机访问安排，不代表已实现多单位
身份、私有资料隔离或新的公网发布能力。当前只供获准研究客户端有界读取。

```bash
python -m pytest -q tests/test_clinical_research.py
```

测试使用合成材料，覆盖原文定位、字段和日期语义、字面转义、有界 PostgreSQL 查询构造、
坏格式及失败响应。测试中的数据库执行是替身；真实 PostgreSQL 和现场限时查询须在
代码交叉审查后由负责人验证。没有用 SQLite 代替 PostgreSQL，也不以请求成功证明研究价值。

本批只增加独立只读路由及投影，不改变原列表、详情、清洗、Worker 或数据结构。
回退本批独立提交后保留原始数据和已有运行保护；不要倒退到移除运行保护前的旧版本。
API 使用 reload 时，切代码前由负责人安排停启；无需因本批改动重启 Data-Fabric Worker。

Git 撤销的是代码。本次不需要恢复旧数据库，也不能用旧数据库或 Redis 快照
覆盖新产生的业务数据。配置、数据和未跟踪文件保持原样；不使用强制重置或清理命令。


## 有界来源采用与历史读取（本轮新增）

这条链只使用独立配置的 PostgreSQL/Redis：来源目录 → 有界公开读取或受控档案导入 → 不可变版本 → 截点/许可读取 → 雷达采用。
没有历史商业档案时，当前公开资料验证接入，明确标记的合成档案验证历史计算；不宣称已完成真实近十年回测。

历史模块需要显式传入以下进程环境变量，不回退旧 `APP.DB_URL`、旧 Redis 或现役数据库：
`DATAFABRIC_HISTORY_DATABASE_URL`、`DATAFABRIC_HISTORY_REDIS_URL`、
`DATAFABRIC_HISTORY_READ_TOKEN`、`DATAFABRIC_HISTORY_ADMIN_TOKEN`、
`DATAFABRIC_HISTORY_SOURCE_ALLOWLIST`（JSON数组）。
新凭据只放私有启动配置，不进入仓库、浏览器或命令输出。API/worker使用同一工作树和这些环境，绑定本地回环。
`DATAFABRIC_HISTORY_DATA_ROOT`预留给外部原件文件；本版JSON/XML原件直接随不可变记录保存在独立PG，不另写一套文件副本。

完成私有配置注入后，从当前工作树执行：

```bash
python -m migration.history
uvicorn api.main:app --host 127.0.0.1 --port 18511
# 另一终端使用相同独立配置；沿用RQ extraction队列，不清队列、不启动旧采集流程
rq worker --url "$DATAFABRIC_HISTORY_REDIS_URL" extraction
```

迁移只创建 `history_sources`、`history_versions`、`history_runs` 三表，原documents/抽取/图谱表不变。
`migration/versions/V017__history_records.sql`也被原迁移器识别；推荐独立链使用上述专用入口，不能对现役库试跑。
关闭本轮API/worker、回到原提交即可停用新增接口；保留独立历史库和新表，不自动删除数据或清队列。跨版本回退前核对在途任务，不能让旧代码接手新任务。

实际接口（均需独立Bearer凭据，导入/采集/恢复只允许管理员）：

- `GET /api/history/sources`：10类业务标签及实际可读来源，未覆盖领域不伪装已有数据。
- `POST /api/history/fetch`：固定ClinicalTrials/PubMed/FDA CRL/Europe PMC的有界只读查询，需 `Idempotency-Key`。返回持久任务，`GET /api/history/runs/{id}`读真实完成/截断/失败。
- `POST /api/history/runs/{id}/retry`：传 `expected_attempt`，先核对原固定RQ job不活动才恢复。Redis状态不明、started/queued任务一律不重发。数据库提交未知时先读任务，不盲重跑。
- `POST /api/history/archives`：固定原站ID、trial/program/outcome/document JSON原件（PubMed也支持XML），需幂等键。服务器生成版本ID和本地时间，客户端不得倒填first_seen/received。
- `GET /api/history/records`：必填 `source_id,is_demo,mode,cutoff`；可给 `run_id`冻结清单、`source_record_ids`交集、至多3个字面AND `terms`，每页1–100。日期/时刻必须保留精度，完整时刻必须含时区。
- `GET /api/history/versions/{version_id}`及 `/raw`：必填相同来源/原站ID/demo/模式/截点绑定；旧版本也重新核当前来源许可。
- `GET /api/history/changes`：采用提交日志，`source_id,is_demo,after,limit,cursor`；不是上游完整变更feed，也不能直接当历史特征。

`current`与`operational_replay`要求该版本本地首次采用不晚于cutoff。
`public_history_reconstruction`只接纳管理员明确审核的归档证明；普通导入、今天读到的旧发表日期均不自动获得历史资格。
证明SHA绑定全包排序后的 `[{source_record_id,raw_content}]` 的UTF-8 canonical JSON，可用 `archive_manifest_hash`生成。
审核记录保存服务器审核人、时间和依据，这是有责任人的人工确认，不表示程序验证了外部机构的真实性或完整历史。
程序会拒绝结局窗口、公开时间或新映射晚于所谓旧档证明的矛盾包；真实证明须HTTPS出处，fixture仅demo。

`run_id`冻结该包的原站ID清单（可显式 `scope_record_ids`），在时间资格和首响应max_seq内选版本。未来补档不能改变已发游标的发布边界。
清单完整性、截点可见条数、缺资料成员和上游是否翻完页分别返回；一页结束不代表站点全量。不同截点应各自使用当时的独立完整清单，不把今天的成功名单回填过去。
后到的旧归档按归档证明时间排序，不以今天入库序号替代源时间。
历史证明的年/月/日范围重叠而不能排先后，或同一可见时点的不同原件缺少不重叠的来源修订时间，均拒绝歧义包保旧；不能用精度上界或本地导入顺序伪造源历史。
同一原件重复采用复用版本，本地first_seen/received不改写；新的读取任务另留created/completed和采用版本清单。

项目映射仅接收有出处的 `program_id,lineage_id,indication,jurisdiction,mapping_available_at`，由管理员显式审核并记录依据，不在这里重做唐蔚雯的身份算法。
结局用独立outcome原件：`event_type,unit_kind,unit_id,event_observed,event_at,observation_start,observation_end,public_available_at`。
没有记录或未知不当阴性；阴性必须明确覆盖窗。后续结局供雷达按独立evaluation cutoff读取，不混入早期特征。

FDA CRL保留application_number原生数组和letter_type，按PDF+信函日期/类型/申请号复合键分开；同批歧义拒绝，不把CRL、撤稿、终止自动解释为疗效/安全失败。
Europe PMC采用source:id、PMID/DOI、摘要、更正关系及许可字段，不把同PMID当独立研究，不自动下载全文；firstPublicationDate可能补算，只保留来源原值。
ClinicalTrials保留实际/预计时间语义和未知人数。新PubMed链用EFetch原始XML摘要；原采集器也不再用ESummary的summary假充摘要、吞掉失败或把固定页数截断当EOF。

后续连续发布改动已接住旧网页/CLI的实际落库：已安装 V019 时，同 UUID 变化正文保存为待发布的新候选，旧缓存和旧成果保留；未安装发布能力时仍拒绝覆盖。API 来源的旧 URL 重采继续明确拒绝，不能先清空唐的抽取/图谱/同步结果。
旧网页后台线程不是新增history持久任务，不把二者混为已完成的全站重采平台。

本轮检查：

```bash
python -m pytest -q tests/test_history.py
# 必须显式注入隔离PG地址（fabric_history@127.0.0.1:18436）；测试使用临时schema并清理
python -m pytest -q tests/integration_history.py
```

真实跨系统回测与公开小批调用由雷达端E2E脚本另外验收；上述HTTP替身测试不冒充外部实际采集或科学预测表现。


### 本分支联调结果（2026 年 10 月 18 日）

在 zktest 独立测试库和 Redis 中完成了真实 HTTP/RQ 联调：ClinicalTrials 一条登记、FDA 两封信件、Europe PMC 一条记录经过采集、保存，再由雷达读取、形成调查并导出。历史教学队列另行检查，包含未来版本排除、迟公开标签、明确阴性窗口、预测固定及后续评分；不以教学数值声称真实多年预测效果。

普通测试套件 247 项通过；最后时间歧义修正后，相关 16 项单元测试和 12 项真实 PostgreSQL 测试通过。这些数量存在重叠，不累加成一个更大的测试总数。跨服务最终 42 项通过，雷达还完成实际浏览器操作和跨旧入口的撤源检查。完整联调记录集中在同机雷达主目录的 `evaluation/handoff-verification.json`。

上面记录的是 10 月 18 日早期隔离联调。其后已合入现役 dev，完成历史库、源库发布待办及持续运行部署；不能再按“尚未切换现役”理解。临时测试进程已关闭，测试数据库保留，现役启动保护继续有效。当前状态和接手任务见本报告入口；撤回代码时先核对新增格式与在途任务，保留历史表和原件，不以删表或清队列回退。


## 唐的图谱接入：固定发布包，不复制图谱算法

本轮沿用同一个历史库、版本ID、管理员导入和读取许可，新增三个固定来源：
`legacy_documents`（已获准旧原文的冻结桥）、`reviewed_graph`（所选审核图）、
`primekg_graph`（当前背景图）。不新增图表、队列、Agent或图后端；
`V018__graph_record_kind.sql`只放宽既有版本类型，旧四种原件和旧哈希不变。
再次执行 `python -m migration.history` 即可在**独立测试库**应用。

先在私有历史配置的来源allowlist中加入需要的派生来源及所有原站点。
原 `source_site` 必须已在history来源目录中并获准，不能用图来源绕过它。
派生目录持久保存服务器核对的 `dependency_source_ids`，读取许可取当前依赖交集；
任一原站撤权，保守停读整个派生来源组，包括旧版本、raw、邻域和名称解析。
当前粒度是来源组，不是已实现逐文档/多机构授权。恢复原源许可后才能重读。

### 旧图只读捕获与续批

发布桥只GET现有文档详情、审核实体/关系和图视图，不触发审核、抽取、合并或WeKnora同步。
它要求明确本机HTTP根地址；不读现役.env或尝试修改现役数据库。
完整UUID清单保存在一个私有JSON数组文件中，不在每个图包重复保存整个项目名单。

```bash
python -m flows.graph_publish capture-legacy \
  --url http://127.0.0.1:8100 --namespace datafabric.zktest \
  --scope-file /private/selected-uuids.json --offset 0 \
  --output /private/graph-batch-0.json
python -m flows.graph_publish publish-legacy \
  --observation /private/graph-batch-0.json \
  --idempotency-key project-batch-0 --receipt /private/publish-batch-0.json
```

默认只取源状态 `approved`。需要检查自动审核资料时，捕获命令可显式加
`--include-auto-approved`；原 `auto_approved` 原样保留且身份不标为人工确认。
旧HTTP仅在人工审核状态并有字典 `is_verified=true` 时给当前已确认身份标志；
审核人/时间未暴露则为null，不补写。它仍不等于机制评价或专业批准。

一批最多6篇、100节点和200边；这不是项目总上限。输出带
`scope_manifest_sha256/scope_record_count/batch_offset/next_document_offset`。
继续时使用同一个完整清单及首次输出的SHA：

```bash
python -m flows.graph_publish capture-legacy \
  --url http://127.0.0.1:8100 --namespace datafabric.zktest \
  --scope-file /private/selected-uuids.json --offset 6 \
  --expected-scope-sha256 <首次回执中的SHA> --output /private/graph-batch-6.json
```

名单发生变化就拒绝续读。每批观测/图包只保留本批ID和完整清单标识。
捕获限时60秒、每响应2MiB、每批8MiB，超过预算或上游失败明确失败；
不会把失败当空结果或无关系。文件以0600新建，已存在则拒绝覆盖。

先冻结原文，再发布图包，两个步骤各有现有history任务和幂等键。
后一步失败时保留前一步原件；回执留document任务ID。遇到结果不明先核对回执，
不自动重发。重新操作必须使用同一冻结观测和原幂等键，不能重新抓取变化内容冒充重试。

### 固定版本读取、身份候选与背景

图原件是 `datafabric.graph.bundle.v1` 根JSON；图版本就是已有history `version_id`。
节点以稳定namespace+源ID标识，entity_dict与extracted_entity使用不同namespace；
未映射mention不会回退成 `dict:<抽取ID>`。默认别名只来自本批已选mention原名，
自动/未核mention即便有提议dict_id也保持独立mention身份，不进入已确认节点的别名或证据。
不披露没有逐来源归属的全字典别名或汇总属性。源合并重定向保留日志指针，
不让雷达重新合并，也不把当前合并/别名倒填到过去。

- `GET /api/history/graph/{version_id}/resolve`：仍必填来源/原站ID/demo/mode/cutoff；
  另外给精确 `name`，或 `external_namespace+external_id`，最多20个候选。
  返回resolved/ambiguous/not_found，绝不按同名自动选第一。
- `GET /api/history/graph/{version_id}/neighborhood`：同样的版本绑定，重复
  `seed_ids=namespace|id`；可筛relation_types、direction，最多2跳/100节点/200边。
  返回原有方向及类型、所选固定子图范围和截断原因，不称为全图所有邻居。
- 原文继续走已有 `/api/history/versions/{id}` 和 `/raw`。
  关系review与binding分开；quote必须在指定版本clean_text字符范围精确匹配。
  不匹配标unbound，不能喂给“已确认关系”计算。关系有效时间未知留null，
  不拿文档日期补成关系发生时间。

当前导出只获得本次服务器采用时间，可在此后做operational回放；
不支持以导入旧proof获得public历史图资格。原文引用在每次图读取时按相同模式/
截点核对，图包资格不能夹带未来原文或未来身份合并。未来真实历史图档案应另加
受控、可审核的导入口，不能复用本次当前导出冒充。

PrimeKG使用同一固定图包契约，可current读取，或在本地first_seen后operational回看当时收到的背景；不允许public_history_reconstruction：

```bash
python -m flows.graph_publish publish-primekg \
  --observation /private/primekg-current-capture.json \
  --idempotency-key primekg-current-capture-1 --receipt /private/primekg-publish-1.json
```

输入是明确只读捕获的原生节点、端点、关系、观察时间和has_more。
不自动连接Neo4j、补历史或补文档证据。PPI/associated with等保持原类型和存储端点；
存储方向不等于因果方向，反向物理重复不当独立研究。当前PrimeKG边只能作背景追问，
不能改名成合作/供应关系来强启现有雷达算法。

现场核查没有人工approved关系；auto_approved实体也不能冒充人工确认关系。
因此真实PrimeKG背景链、真实旧资料的未核/缺口、合成已核证据链分别验收。
这不表示唐的身份/时态工作已完成，也不表示机制专业评价已经接通。

旧WeKnora写推送保持原样：其dict ID fallback、来源字段丢失和Go投影tenant/KB隔离
缺口尚须单独治理。本轮不清现图、不改旧writer发布身份，也不把HybridSearch工具
冒充能返回结构边的图provider。

图相关检查：

```bash
python -m pytest -q tests/test_reviewed_graph.py
python -m pytest -q tests/integration_history.py
```

后者使用明确隔离PostgreSQL临时schema，检查精确引用、未知/真实失败、跨来源撤权、
伪旧proof、未来原文、namespace、demo和背景边界；普通HTTP替身不冒充现场数据验证。
撤回代码前停用新图发布，并从旧服务的来源allowlist移除 `reviewed_graph`、
`primekg_graph`、`legacy_documents`。保留原件、版本表及CHECK扩展，不收窄CHECK、
不删库/清队列；旧四来源按原接口继续读取。旧版0a4b9b8不能解析新graph类型，
不能让它继续读取上述新增来源。

### 可重复的PrimeKG只读捕获

不再需要手写observation。复用现有httpx和现场已验证的Neo4j HTTP事务路径，
私有环境显式设置 DATAFABRIC_GRAPH_NEO4J_URL/USER/PASSWORD，DATABASE默认neo4j。
地址必须本机回环，凭据不写进参数或输出，也不读取现役.env。建议配置只读账户；
命令只发送固定参数化MATCH及Access-Mode: READ，不接受任意Cypher、不写图。

```bash
python -m flows.graph_publish capture-primekg --node-type gene/protein \
  --node-source NCBI --seed-id 1956 --neighbor-term lung --limit 10 \
  --output /private/primekg-egfr-current.json
```

当前明确支持gene/protein、disease、drug三类种子；一跳最多200边，加读一条识别has_more。
命名空间+ID命中多个物理节点时拒绝，不自动选第一个；明确空邻域、未找到种子和查询失败分开。
2MiB/20秒客户端预算，事务只包含固定读取，不声称已经设置服务器全局限时或完整图快照。
输出先以0600新建且不覆盖，再用独立publish-primekg命令发布，捕获本身不写业务库/图。
采用服务器first_seen之前不可回放；之后可回看冻结背景版本，仍无关系真实有效时间或专业证据。
[Neo4j HTTP读取格式](https://neo4j.com/docs/http-api/current/query/)及
[READ路由说明](https://neo4j.com/docs/http-api/current/routing/)是当前兼容路径参考；此命令不升级现役图服务。

## 落库后自动发表与雷达增量衔接

本轮把原有网页、CLI、API上传和批量SQL的实际写库接上不可变版本，不再要求每批手动capture。
原流程继续采集、清洗和质检；只有原数据库事务真正提交的内容才会进入待发表。
文本先发表，不等待实体抽取或图审核。图审核/身份资格变更后另发固定图版本。

### 启动与配置

由负责人在维护窗口确认源库连接、备份和代码版本。源库与历史库是两个显式配置，
不复制现役.env，不把URL/密码写进命令行或日志。源连接可由服务器私有配置复用真实APP.DB_URL；
不要把已经编码的密码再次按明文拼接。所需变量为：

- DATAFABRIC_PUBLICATION_DATABASE_URL：原采集库完整SQLAlchemy URL。
- DATAFABRIC_PUBLICATION_SOURCES：允许发表的原source_site JSON数组。
- DATAFABRIC_PUBLICATION_IS_DEMO：服务端true/false；教学源库必须true。
- DATAFABRIC_PUBLICATION_NAMESPACE：此生产实例稳定命名空间。
- DATAFABRIC_PUBLICATION_MIN_FREE_BYTES：默认预留15GiB；DISK_PATH指定实际数据所在文件系统。
- 原DATAFABRIC_HISTORY_*：仍指独立历史库、来源许可和独立凭据。

~~~bash
python -m migration.publication
python -m migration.history
python -m flows.history_publish --poll-seconds 30 --batch-size 20 --backfill-batch-size 1
~~~
存量补录与发表预算分开：初期每30秒至多新增1篇存量待办，同时最多消化20项已有待办；有可处理的pending/processing待办时先处理新提交和积压，不继续复制旧库存。失败/隔离行保留并按原规则核对重试，不作其它库存的永久全局闸门；同一文档仍不能越过未完成版本。持续繁忙时存量会延后，不能报告完成。改成 --backfill-batch-size 0 可暂停补录并继续处理已捕获资料；恢复同一清单会从原游标继续。调速需先看 pending、磁盘和单条复制体积，不按百万库存一次启动全量。


V019只在显式源库新增publication_outbox和固定写入触发器，不在迁移里扫描百万库存。
迁移的锁等待上限3秒，遇占用则整笔回滚。publisher是普通常驻循环，不增加第二队列；
雷达计算仍使用其已有RQ。启动/停止publisher都不清现有采集或抽取队列。

既有clinicaltrials、fda_api_drug_label、chictr、fda_api_enforcement、nmpa、cde_xxgk、cde
可逐源许可后自动桥接。额外六类目录只声明已有库存，不冒充新增公开抓取接口。
unknown或未许可来源保留blocked待办与原因，不借legacy_documents绕过许可。

### 数据怎样保存

documents事务内冻结正文、标题、原站和状态；doc_extra与该事务一起提交或一起回滚，
临床事实只从已冻结正文的精确protocolSection片段解析，不借稍后可变extra补事实。
source_projection与原MD片段、Unicode定位和SHA必须相符。旧v1原件/哈希不改写；
自动发布使用datafabric.collected.document.v1。普通正文、临床原生字段和结构化图包分别处理。
新采用的正文投影v0.2只剥离文首完整且原站匹配的采集元数据头，并将真实HTML表格转为单元格文字；原MD、SHA及临床协议定位不变，clean_text字符位置不是原MD位置。图证据只在目标clean_text里唯一精确定位，缺失/歧义保持未绑定。旧版本和旧分析不改写；清洗规则修正后的重投影使用现有archives导入同一原件生成新版本，不作为新的独立研究。


同UUID新清洗正文先作为新候选持久保存，不覆盖仍挂着旧抽取/图谱的兼容缓存。
写入结果明确legacy_cache_retained/publication_pending。连续重复不新发，
A→B→A或撤回后恢复仍记录新的本地观察，原正文SHA相同不等于新独立研究。

初始库存按固定max(id)和keyset小批推进，已捕获的UUID不从旧缓存重发覆盖新候选。
图若需要尚未发表的旧原件，只标context_reference供精确引用，不进入当前正文头版。
审核dirty可合并为实际读取时的当前图，不声称捕获每次中间审核。字典撤销is_verified/
is_deleted资格也会对实际引用文档产生图待办；不改唐的消歧或全局别名算法。

源删除产生显式withdrawn状态；审核撤回产生少边/空边图版本。没有资料、抓取失败或没有图
均不推断删除，更不推断临床失败。业务撤回与当前来源许可分开，旧版读取仍检查来源许可。
changes继续是采用日志，current_version_ids在固定max_seq内给当前头版，context-only时为null。

publisher先在源库持久冻结准备输入，再跨库用固定幂等键发表，最后确认并压缩源待办为SHA和版本回执。
history已提交但源ACK丢失时，下次核对同key/同输入，不重抓变化内容。不宣称跨库事务原子。
每文档按待办顺序处理，失败保留旧版与待办，不跳到后来正文冒充当前版本。
初始库存范围、尚未走完的位置和资源暂停均明确可查，不把首批成功称为全库完成。

### 状态、检查与回退

GET /api/history/publication/status?source_id=legacy_documents（或reviewed_graph）使用已有read凭据，
返回服务端is_demo、namespace、源库名称、真实last_heartbeat、状态计数、初始库存位置和磁盘门槛。
无心跳、过期、许可失败、资源暂停与当前没有新资料分开。接口不返回待办正文或密码。
低于磁盘门槛停止新增history复制并保留pending；已有成功回执可以继续核对，需运维处置空间/生产限额。

~~~bash
python -m pytest -q tests/test_publication.py
python -m pytest -q tests/integration_publication.py tests/integration_history.py
~~~

后者要求显式隔离PG18436，在临时schema实际检查触发器、两库提交/失败恢复和权限，不用SQLite。
跨服务持续验收由雷达实际HTTP上传与常驻publisher/monitor/RQ完成，测试不直接调用tick制造成功。

回退前停止publisher和雷达monitor，保留待办/回执/原件；按维护窗口停用publication_*四个源表触发器，
不删表、不清队列、不还原旧数据库覆盖新数据。未安装V019的环境保留原changedbody拒写行为。
退到不能消费新collected原件的客户端时先停其新来源消费，其他旧来源按原许可运行。

当前单条ArchiveRecord.raw_content须同时满足1,048,576字符及1MiB UTF-8字节预算，外层冻结包装也计入预算。明确超限保存publication_record_too_large及字段/上限/单位，结构校验失败保存publication_input_invalid；两者保留原待办和旧版、标blocked并至少1小时后核对重试，同文档后继不越过，其他文档继续处理。大原件需按证据定位分块或通过已有原件存储扩展接入；当前不自动截断、不冒称已经处理成功。


## 长期专题所需的连续来源与健康信息

原采集写入仍通过原事务待办发表，雷达不用读源数据库。库存补录在原控制记录内按已许可的 source_site 轮转，各站保留固定 max_id、独立游标和完成状态；每轮总预算由 --backfill-batch-size 限制，不乘来源数。旧全局水位只在同一许可站点清单内迁移。这里的完成只表示最初冻结库存已遍历，不代表原站全量或持续抓取正常。

百万级源表先独立建立 keyset 索引，不能在应用启动或大事务里做。必须提供原先显式的 DATAFABRIC_PUBLICATION_DATABASE_URL；命令不读取默认 APP 数据库：

~~~bash
python -m migration.publication --site-index
# 仅回退本次索引，保留资料、待办和已捕获版本
python -m migration.publication --drop-site-index
~~~

GET /api/history/source-health?source_id=clinicaltrials 使用原读取令牌及当前来源许可，分别返回：真实 crawl_log 中的 scoped 检查/成功/失败（没有记录就是 unknown）、发表器心跳和该源未完成待办数、按站库存进度、固定刷新任务。不会返回原配置或异常正文。日志里的 success、空 changes、发表器 idle、库存 EOF 都不证明原站完整或领域平静。

WHO DON 的官方公开 OData 已实际只读验证可达；who_don 保留原生 Id/DonId、公告发布/更新时间、完整响应原件和字段 JSON pointer。正文独立规范 HTML。DON 是选择性公告，公告发布日期不是暴发开始，不从叙述猜病例数或发病率。CDC 此环境只读请求曾返回403，未接入；SEC 仍缺明确发行人队列与合法 User-Agent 配置，未接入。现有 ClinicalTrials 库存继续沿原收集器，不另开全库重复采集。

连续刷新复用现 publisher 循环和原 history RQ，不新增服务/队列。服务器显式设置以下 JSON 到 DATAFABRIC_HISTORY_WATCHES（默认 []，不开额外抓取）；同时把来源加入 DATAFABRIC_HISTORY_SOURCE_ALLOWLIST，并运行 python -m migration.history 注册目录。以下只是受限范围，不是所有疫情/论文：

~~~json
[
  {
    "id": "who-recent",
    "request": {"source_id":"who_don","query":"*","max_records":5,"max_pages":1,"deadline_seconds":30},
    "cadence_seconds":3600,
    "lookback_days":30
  },
  {
    "id": "epmc-bcma-updates",
    "request": {"source_id":"europe_pmc","query":"BCMA AND myeloma","max_records":10,"max_pages":1,"deadline_seconds":30},
    "cadence_seconds":3600,
    "lookback_days":30
  }
]
~~~

WHO 每轮固定最近窗口和截止，按 (LastModified,Id) keyset 续页；Europe PMC 为固定主题附加官方 UPDATE_DATE 日期窗，再续原生 cursor。这是数据库更新日，不用首次发表日代替，以便看到旧文更正。完整 query/时间窗/续页在原控制行保存，页读完才前进，EOF 后再开始下轮重叠刷新；长时间停机/来源事后倒填超出窗口仍是缺口，不宣称完整连续监测。公开 API 也不是事务快照，complete=false 始终保留；EOF 只是当轮查询结束。固定抓取 run 永远是真实非demo，不能把真实 WHO/论文改为教学资料。

未知提交先核固定回执，原RQ任务已不活动才能恢复。失败、许可撤回或低磁盘保留续页并在健康信息显示；已有真实原件不丢失。首次收到时间仍由服务器生成，当前抓取不自动取得历史证明。修改 watch 查询/窗口策略时应使用新明确 id 开始新范围，不就地修改活动游标。

外部同口径观测仍用现 /api/history/archives 的 external_archive、record_kind=document，原件固定为 datafabric.observation.v1，模型见 biz_rules/observations.py。仅支持 metric / event：来源对象ID、指标定义版本、十进制区间/单位、地域/人群、半开期间、reported/estimated/target、可空分母、原生时间、active/withdrawn 和完整 supersedes 引用。未知不补零；未来 reported 期间不当已发生。没有真实商业/病例数据时只能保存明确的教学或声明输入，不能展示为真实业务指标。

basis.kind=declared 是声明；source_bound 必须逐非空字段与另一份已采用原件的完整版本/哈希及严格 JSON pointer 对应，不能引用另一 observation 循环自证。数值按十进制原精度比较。只有服务端校验成功才能发表 datafabric.observation.projection.v0.1；字面绑定不等于真实性、独立性、因果、专业评价或口径可比。读取时继续核对当前来源许可及同模式/截止时间下的原件资格。修订须保持对象/指标定义/单位/地区/人群口径，期间可真实纠正；旧版本和旧判断保留，撤回与来源撤权分开。

本次不改旧记录/旧 parser 或原始正文。回退时先关闭 DATAFABRIC_HISTORY_WATCHES 并停用新专题消费者，再回到已验收提交；保留新增版本/控制行。旧回填代码可从兼容 last_id=min(各站游标) 重新扫过，已捕获事件仍去重；无需删除资料或收窄数据库约束。WHO 和 typed observation 的新增能力在旧代码中不承诺可读/可执行，恢复新版后再启用。日常仍保留低磁盘底线，不能为补库存关闭该保护。

公开依据：[WHO DON](https://www.who.int/emergencies/disease-outbreak-news/)、[WHO 官方API字段](https://www.who.int/api/default/diseaseoutbreaknews/sfhelp)、[Europe PMC 官方查询字段](https://dev.europepmc.org/docs/EBI_Europe_PMC_Web_Service_Reference.pdf)。这些公开接口说明不是来源覆盖或本系统分析质量证明。

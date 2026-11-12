# ZK_Data Fabric — 数据基座 ETL

面向药学领域的数据采集与处理流水线，支持网页爬取、本地 PDF 批量解析、AI 结构化提取、质量门控与人工审核回灌。

## 版本使用

`integration/rebuild-2026-11` 是重构线（`feat/t26-clean-v12`）与团队分支 `feat/server_fix`（已含 `dev`）的合并分支，**待团队评审、未被接受**，尚未进入 `main`，也没有部署，不替代团队主线；合并后并非团队全部功能都在运行（药名与标识符对齐库保留但未接入，History 来源记录的规则抽取退回模型抽取），功能边界和未完成项见 [docs/decisions/0003-integration-open-items.md](docs/decisions/0003-integration-open-items.md)；现役目录 `/home/zk_test/zk_projects/ZK_Data-Fabric-test` 仍按原分支运行。每处合并冲突怎样处理、哪些团队改动没有带过来、迁移为什么改号，见 [docs/decisions/0001-merge-server-fix.md](docs/decisions/0001-merge-server-fix.md)。不能因为测试分支通过，就认为学生使用的服务已经更新。不要在现役目录复制环境文件、清队列或执行数据库迁移。

团队 10 月 31 日一轮改造（分支 `codex/fabric-opus-20261031`，基线 `82bad7b`）的重点是让“资料里写了什么”“系统猜它是谁”“人工确认了什么”分得清楚。主要变化：

- ClinicalTrials 和已确认结构的 FDA 字段先按规则读取，不再让模型重抄编号。名称、别名、相似度和模型判断只提候选；没有人工标注集，阈值仍未校准。
- 重复出现只增加来源记录，不再自动审核通过。机器审核保存建议；真正确认需要服务端登记的审核人、固定原文和明确依据。
- 接受一条链接提案只调整这一条记录。合并整个字典是另一项明确操作，支持核对改动和撤销。
- 正文、更正指向、材料类型、来源时间及原文位置随版本交给雷达。新闻标题、摘要、全文分开；社媒按未证实说法保存。
- 图查询的 `reviewed` 范围要求身份与关系有可核对依据。查看未核候选时显式使用 `with_candidates`；旧版本仍可读取。
- 文档正文进 WeKnora 只走 History 索引发布（`db/index_publication`）；旧的文档同步接口和批量同步线程已删除。

详细修改和接手事项见下面的同一份修改报告，历史记录按日期保留。

徐子雯组本阶段的开发安排见[数据采集工作说明](task_doc/数据采集工作说明.html)（下载后用浏览器打开）。其中列明数据用途、来源任务、现有代码位置、雷达交接、验收办法和研一任务拆分；本阶段任务以这份说明为准。下面的修改报告继续保留既有改动记录和技术说明。

徐子雯、唐蔚雯两组接手先读[数据采集与图谱修改报告](task_doc/数据采集与图谱修改报告.md)：改动范围、旧程序行为变化、具体任务、检查和回退集中在这一份。下文保留详细接口与运行说明。

---

## 环境要求

- CPython **3.10**（现役 Fabric 的解释器是 3.10.12；`requirements.lock` 按 3.10、x86_64-unknown-linux-gnu 解析）
- uv **0.13.0**（在 PATH 上，例如 `~/.local/bin/uv`；或者 `make UV=<路径>`）

---

## 快速开始

### 开发环境与测试

唯一的入口是 `Makefile`，依赖只从 `requirements.lock` 安装：

```bash
make venv              # .venv：只按 requirements.lock 安装（含契约 wheel），每个包都校验 sha256
make test              # 单元测试 tests/test_*.py（不连数据库）
make test-integration  # tests/integration_*.py：临时 PostgreSQL（paradedb）和 Redis 容器，跑完按名字删除
make lint              # ruff（pyproject.toml）
make metrics          # 函数长度 80 行 / 圈复杂度 20 的门槛（ops/code_metrics.py；例外清单 ops/metrics_exceptions.txt 只许变短）
make check-install     # 一个用完即删的新环境：按锁文件安装，再逐个导入全部模块
make check-lock        # requirements.lock 是否仍与 requirements*.txt 一致（要访问包索引）
```

`make check-lock`、`make lock` 要访问 PyPI（`make venv`、`make check-install` 在 uv 缓存里没有的包也要下载）。zktest 上直连 PyPI 有时超时，这时只给这几条命令临时加上本机已有的 HTTP 代理（mihomo，监听 127.0.0.1:7890），例如 `HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 make check-lock`；不要把代理写进全局配置。`check-lock` 的结果有两种失败：解析本身失败（包索引或网络不可用、依赖解不出来）时报 `check-lock did not run`，make 显示 `Error 2`，锁文件没有比较过，不能因此重做锁；只有解析成功而内容不同时才报 `requirements.lock is stale`（make 显示 `Error 1`）。两种情况 make 都以非零状态结束。

- 直接依赖写在 `requirements.txt`、`requirements-contract.txt` 和 `requirements-llm-transport.txt`；`requirements.lock` 是由它们解析出的完整锁，每个包都带 sha256（锁文件头一行写着解析命令）。改了依赖后运行 `make lock`，再提交锁文件。国内镜像用 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple make venv`，哈希照样校验。
- 抽取一致性的共享契约 `extraction-contract`（BioCheck 协议的字段集合、严格回执校验、规范序列化）的版本和哈希以 `requirements-contract.txt` 为准，这里不重复写（多出的 `mech_v2` Fabric 不用）；Fabric 用到两个核对器：编号和画像注释用 `biocheck.extraction_rules.v3`（契约 `CONTRACT`，BioCheck `/v1`），测量用 `biocheck.extraction_rules.v17`（契约 `CONTRACT_V3`，BioCheck `/v3`），外加 `canonical`、`verdict`，以及测量规则读数字、单位和期间用的 `normalize` 里的当前几个函数（`decimal_text(_v6)`、`is_whole_number(_v6)`、`unit_subject_head_v4`、`period_span_whole_v5`）；契约包不再描述别的核对器，库里存着的旧核对器结果带着它们各自的版本号，只读不重算。它不在包索引上：先在旁边的 extraction-contract 检出里执行 `bash scripts/build_and_verify.sh`（离线构建可复现的 wheel，并校验同一哈希），`make venv` 从 `../extraction-contract/dist`（或 `EXTRACTION_CONTRACT_WHEELS=<目录>`）取它，哈希和 `requirements-contract.txt`、`requirements.lock` 里的相同。
- 模型调用的共享传输层 `zk-llm-transport`（导入名 `llm_transport`）固定 **0.1.1**（wheel sha256 `7ef33b72…`）：加工层的测量抽取经它发每一次 HTTP 请求，请求前、结果后各有一个回调，由 Fabric 自己落库（db.measure_runs）；旧抽取和消歧的两个非流式对话调用（`core_engine/cloud_api_client.py`）也经它发送。流式输出、embedding 和 LangChain 的工具调用 Agent 仍走 OpenAI SDK，原因见 `docs/decisions/0007-llm-transport-chat-calls.md`。它同样不在包索引上：在旁边的 llm-transport 检出里 `make build`，`make venv` 从 `../llm-transport/dist`（或 `LLM_TRANSPORT_WHEELS=<目录>`）取它。
- `make test-integration` 由 `ops/run_integration_tests.sh` 执行：容器名 `fabric-itest-<角色>-<随机>`（`ITEST_PREFIX` 可改），带本次运行的标签 `fabric-itest.run=<随机>`；只登记 docker 实际为本次运行创建的容器 ID（连同镜像和匿名卷）到清单 `.test-artifacts/fabric-itest-<随机>.containers.jsonl`（检出里，git 忽略）（`ITEST_MANIFEST` 可改，跑完保留），结束、失败或被中断时都按清单逐个核对标签后停止并 `docker rm -v`（连匿名卷一起删，不扫描、不清理别的卷），不碰不是本次创建的容器；只绑定 127.0.0.1 上 19871 以上的空闲端口；要求固定端口（18436、18636、18736、18737）的几组测试在私有网络命名空间（`unshare -rn`）里跑，经 unix socket 转发到容器，不占用主机上的这些端口。真实 BioCheck 规则的测试用 `BIOCHECK_SRC`（默认 `../clean-biocheck`，先在那里 `make venv`）；找不到检出时这些测试显示为跳过。也可以只跑其中几个文件：`bash ops/run_integration_tests.sh tests/integration_lease.py`。
- 已验证的组合：uv 0.13.0、CPython 3.10.12；通过数量以 `make check` 的输出为准，这里不写数字。
- 已有环境里只做导入检查：`python ops/check_imports.py`。
- 各来源真抓一遍、打印一张表：`python ops/check_sources.py [来源…] [--twice | --rounds N] [--incremental]`（`--twice` 抓两次，查重复存版本，`--rounds N` 抓 N 次；`--incremental` 按固定刷新的读法抓：连续读到 3 条已存且没变的就停，第二次起应该停在 reached_known）；它会写库，`DATAFABRIC_HISTORY_DATABASE_URL` 要指向临时库（先 `python -m migration.runner --db history --apply`），用法和环境变量见文件开头。
- 把一个来源的历史存量一次读到底：`python ops/backfill_source.py <来源> [--query …]`（不设条数上限，每段 100 条，按页往后读到来源读完；中途中断、再跑同一条命令就接着读，已读完的段不再发请求；续读位置没前进而且一个版本都没读到、或碰到出站上限，命令停下并说明；列表网页站点默认只读站点配置里的 `max_pages` 页，`--pages N` 盖过它；`--deadline` 5 到 120 秒，进每一段的任务键）；同样会写库，要指向临时库或有意要补的库，用法见文件开头。

## 运行

想读懂采集，从 `pipeline.py` 读起：一份资料从抓取到能被检索，一步一个函数，按顺序往下读就是整条链路。

数据链路只有一条：采集框架把原件写成只追加的 history 版本，加工层按版本产出派生结果，
正文经 `db/index_publication` 和桥接服务进 WeKnora。

| 层 | 入口 | 说明 |
|---|---|---|
| 来源目录 | `collection/catalog.py` | 每个来源一份声明（插件、限速组、增量键、是否实测）；调度、许可目录、加工层都读它 |
| 采集 | `POST /api/history/fetch`、`db/source_refresh.py` | 读取插件见 `biz_rules/history_sources.py` 的 `PLUGINS`；网站和站点接口统一走 `collection/site_crawler.py` |
| 附件 | `flows/process_attachments.py` | 附件是独立版本，正文由 `processing/attachment_parse.py`（MinerU）产出 |
| 清洗 | `flows/clean_stage.py` | `processing/clean_text.py`、`identifiers.py`、`profile.py`，分块租约并行 |
| 索引 | `db/index_publication.py` | 唯一的进 WeKnora 路径；操作入口见下 |
| 派活 | `flows/scheduler.py` | 定时派采集任务（`db/source_refresh.py`）、发表审核图（`db/publication.py`） |

本地导入文件（原 `--mode local`）用 `POST /api/history/archives`；重抓页面用 `query=by_url` 的读取请求。

正文索引的操作入口：前端“同步 → 正文索引发布”（`/index-publications`），或直接调
`GET /api/history/index/counts`、`GET /api/history/index/status?state=blocked&source_id=…`（`next_cursor` 续页）、
`POST /api/history/index/{id}/retry`（History 管理员凭据）。发送和失败退避由发表循环里的 `index_tick` 自动做；
连续失败 6 次的作业变为 `blocked`，修好 `error.code` 指出的原因后再重试。细节见 `api/README.md`“正文索引发布”。

两个字符计数（只记，不设上限，也不据此跳过重排或拒绝上传；上传仍受原有的在途预算）：`GET /api/history/index/scheduler` 里每个知识库的 `uploaded` 是已发出的物理上传次数 `uploads`、上传正文字符数 `chars`（含桥接证明首传被拒而重发的部分，另列 `retried_uploads`、`retried_chars`）和没有记录大小的条目数 `unsized_entries`；`embedding_requests` 写 `unknown`，WeKnora 自己分批、重试嵌入，不回报。检索回执 `execution.rerank` 记 `provider_calls`（每个知识库和范围一次，也就是一批）、`documents`、`input_chars`（每段截到 8000 字符之后发给重排模型的字符数）和 `retries`；Fabric 自己不重试，桥接是否重试不回报，所以发过调用就写 `unknown`。Fabric 不保存检索的累计值，累计由保存各次检索回执的一方相加。

已删除、不要再用的旧入口：WeKnora 文档同步接口（待同步列表、单篇/批量同步、文档同步统计）、文档重爬与上传接口，
以及旧的 ETL、WeKnora 同步、WeKnora 重解析、反馈导入命令行脚本。正文进 WeKnora 只走上面的索引发布；新资料只经采集框架入 History。

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

一个网站接进来要做六件事，前三件是写代码，后三件是让库和测试认识它、再对原站验证：

1. 在 `config/sites/` 加一个站点配置（`SITE_KEY` + `CONFIG`：列表入口 `start_urls`、选择器、翻页、详情页提取函数；
   接口站点写 `source_type: "api"` 和拉取函数 `api_fetcher`）；
2. 通解读不好详情页时，在 `biz_rules/extractors/` 写一个提取函数，配置里的 `detail_extractor` 指向它（可选）；
3. 在 `collection/catalog.py` 加一个 `_site(...)` 调用：来源编号、名称、证据类型、服务的业务判断、限速组和间隔、覆盖说明。
   `verified` 一律是 False，对原站实测之后由主持人决定改不改；列表不是从新到旧、不能“遇到已存的就停”的，
   在 `_NOT_WATCHED` 里写原因，它就不进固定刷新；
4. 在 `tests/fixtures/source_catalog_golden.json` 加这个来源的许可项（站点来源的许可项由目录生成，`tests/test_source_catalog_golden.py`
   会指出少了哪一项；先看生成的内容对不对，再写进固定文件）；
5. 让库认识它：`python -m migration.runner --db history --apply` 给目录里的每个来源补一行 `history_sources`（已有的不动）；
   `DATAFABRIC_HISTORY_SOURCE_ALLOWLIST` 里加上这个来源编号；要定时刷新的，再把它写进 `DATAFABRIC_HISTORY_WATCHES`；
6. 对原站实测：`python ops/check_sources.py <来源> --rounds 3 --incremental`（连抓三轮，看版本、标题、正文、日期有没有读全，
   第二、三轮有没有重复存），再按 `tests/test_site_crawler.py` 的写法补契约样例测试。

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
make test
```

真实数据库/队列检查需要新建、用完可删除的本机 PostgreSQL 和 Redis（`make test-integration` 会自己起好再删掉，见「开发环境与测试」）。
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
- `POST /api/history/fetch`：ClinicalTrials/PubMed/FDA CRL/Europe PMC/WHO DON/SEC/GDELT 的有界只读查询，需 `Idempotency-Key`。返回持久任务，`GET /api/history/runs/{id}`读真实完成/截断/失败。
- `POST /api/history/runs/{id}/retry`：传 `expected_attempt`，先核对原固定RQ job不活动才恢复。Redis状态不明、started/queued任务一律不重发。数据库提交未知时先读任务，不盲重跑。
- `POST /api/history/archives`：固定原站ID、trial/program/outcome/document JSON原件（PubMed也支持XML），需幂等键。服务器生成版本ID和本地时间，客户端不得倒填first_seen/received。
- `GET /api/history/records`：必填 `source_id,is_demo,mode,cutoff`；可给 `run_id`冻结清单、`source_record_ids`交集、至多3个字面AND `terms`，每页1–100。日期/时刻必须保留精度，完整时刻必须含时区。
- `GET /api/history/versions/{version_id}`及 `/raw`：必填相同来源/原站ID/demo/模式/截点绑定；旧版本也重新核当前来源许可。
- `GET /api/history/version-differences`：必填 `source_id,is_demo,mode,cutoff,version_ids`（至多100个）；每个版本与它上一个可见版本比，只给事实：`serialization_only`（原件只差序列化）、`projection_only`（原件相同，解析器或投影不同）、`raw_changed`（原件变了）；另附解析器、正文、来源更新时间、其余结构字段是否变化。不做判断，也不存储。
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
PubMed全文要设环境变量 `DATAFABRIC_UNPAYWALL_EMAIL`（你们自己的联系邮箱，Unpaywall 要求请求带邮箱；系统不代填）才取得全：PMC 全文只在许可明确（CC/公共领域）时保存，PMC 取不到、文献有 DOI 时才去 Unpaywall 找开放获取 PDF，这一步没有邮箱就不做，覆盖记录里记 `unpaywall_not_configured`，这些文献只有摘要。`DATAFABRIC_PUBMED_FULLTEXT=0` 可整体关闭全文。

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
（已过时：其中的 API 上传、重爬接口和旧 ETL 命令行入口已于 2026-11 删除，见“运行”一节。）
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
python -m migration.runner --db publication --apply   # 只在发表库不是 app 角色建过的库时才跑，见“迁移执行器与来源能力”
python -m migration.history
python -m flows.scheduler --poll-seconds 30 --batch-size 20
~~~
每一轮先做来源刷新，再最多发表 --batch-size 项已捕获的待办。补录旧库存的功能已删除（旧文档冻结桥已退役，它每轮都直接返回“已退役”，从不做事）。失败/隔离行保留并按原规则核对重试，不作其它文档的永久全局闸门；同一文档仍不能越过未完成版本。调速需先看 pending、磁盘和单条复制体积，不按百万库存一次启动全量。


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

原采集写入仍通过原事务待办发表，雷达不用读源数据库。库存补录（按 source_site 轮转冻结库存）已删除；库里留下的 kind='backfill' 控制行只读保留，仍出现在发表状态的 backfill 字段里。

百万级源表先独立建立 keyset 索引，不能在应用启动或大事务里做。必须提供原先显式的 DATAFABRIC_PUBLICATION_DATABASE_URL；命令不读取默认 APP 数据库：

~~~bash
python -m migration.runner --db publication --online site-index
# 仅回退本次索引，保留资料、待办和已捕获版本
python -m migration.runner --db publication --online site-index --drop
~~~

GET /api/history/source-health?source_id=clinicaltrials 使用原读取令牌及当前来源许可，分别返回：真实 crawl_log 中的 scoped 检查/成功/失败（没有记录就是 unknown）、发表器心跳和该源未完成待办数、按站库存进度、固定刷新任务。不会返回原配置或异常正文。日志里的 success、空 changes、发表器 idle、库存 EOF 都不证明原站完整或领域平静。

WHO DON 的官方公开 OData 已实际只读验证可达；who_don 保留原生 Id/DonId、公告发布/更新时间、完整响应原件和字段 JSON pointer。正文独立规范 HTML。DON 是选择性公告，公告发布日期不是暴发开始，不从叙述猜病例数或发病率。CDC 此环境只读请求曾返回403，未接入；SEC 仍缺明确发行人队列与合法 User-Agent 配置，未接入。现有 ClinicalTrials 库存继续沿原收集器，不另开全库重复采集。

连续刷新复用现 publisher 循环和原 history RQ，不新增服务/队列。服务器显式设置以下 JSON 到 DATAFABRIC_HISTORY_WATCHES（默认 []，不开额外抓取）；同时把来源加入 DATAFABRIC_HISTORY_SOURCE_ALLOWLIST，并运行 python -m migration.history 注册目录。以下只是受限范围，不是所有疫情/论文：

~~~json
[
  {
    "id": "who-recent",
    "request": {"source_id":"who_don","query":"*","max_records":50,"max_pages":1,"deadline_seconds":30},
    "cadence_seconds":900,
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

## 本次新增字段怎样使用（2026-10-31）

`HistoryRecord.document_evidence` 说明材料类型、正文完整程度、同源关系和更正指向。外部归档正文放在原件 JSON 的 `full_text`；仍可只交摘要，但不能把摘要标成全文。`document_relations` 的 `corrects/retracts/supplements/reposts` 指向来源记录，已知版本再填 `target_version_id`，不知道留空。同一 DOI 或 PMID 的跨库资料不能算两份独立研究。社媒、转载保留 `unverified_report`；删帖时点未知就留空，不能用发帖时间替代。

审核接口中的 `evidence` 是全部已记录来源；`current_review_evidence` 才是与当前审核人、审核时点和对象绑定相符的那次审核依据。两者不能混用。`identity_confirmed_*` 属于这条记录，`dict_context.verified_*` 属于字典；旧 `is_verified=true` 不能替代这些信息。原 Markdown 的位置还可能包含 `locator.container_json_pointer=/original_raw_content`，先取该字段，再按 Unicode 字符下标切片。

涉及确认、提案采纳、整对象合并、关系审核和字段裁决的操作使用 `X-Review-Token`。服务端通过私有环境变量 `DATAFABRIC_REVIEW_TOKENS` 将令牌映射到审核人，不接受客户端填写一个名字便获得确认资格。配置未提供时明确拒绝，密钥不写进源码或浏览器页面。

新来源 `gdelt_doc` 只返回标题、原网址及索引元数据，不提供新闻全文；其 `seendate` 是 GDELT 的观察时间，不是原帖发布时间，也不是本机的首次采集时间。接口无续页：给定起止时间的读取按 UTC 整点切成 1 小时窗，窗口返回满额（达到请求的 `max_records`）就对半拆开再读，最细 15 分钟，每查询每小时最多 7 次请求；最细窗口仍满额的标 `possibly_truncated`。读取运行的 coverage 里 `window_coverage` 逐窗口写区间、返回条数、是否满额、请求数和未读完的原因，`truncated_intervals` 列出可能被截断的区间；少于上限只说明没碰到上限，不等于窗口读全。实际探测曾返回 429，故不能声称当前服务器已经稳定采通。资料使用需注明 [GDELT 项目](https://www.gdeltproject.org/about.html#termsofuse)。ProMED 尚无所需许可；各级卫健委网页尚未逐站验证；实时社媒商用接口尚未选定。这些都不能写成已覆盖。

`gdelt_doc` 可以进固定刷新（`DATAFABRIC_HISTORY_WATCHES`，每个查询一条，`lookback_days` 设为 1：首轮没有检查点，按它回看）。每轮读 [检查点, 当前整点)，不读还没走完的这一小时，不回看重叠。读到最细窗口仍满额的区间只留在读取回执里（`truncated_intervals`），不重试，检查点照常前进；截止时间或出站额度用完时，检查点停在第一个没读到的窗口起点，下一轮从那里继续，这一轮不记 `last_success_at`。

没有更新时间可过滤、但列表从新到旧的来源（目录里的增量键 `stop_at_known`）也能进固定刷新：`pubmed`、`chictr`、`nmpa`、`cde`、`cde_xxgk`、`cde_zdyz`、`phirda_*` 四个、`fda_press`、`fda_drug_safety`、`fda_api_drug_label`、`fda_api_enforcement`、`fiercepharma`、`roche`。watch 的写法和别的来源一样（`request` 里给来源和读取入口：网页站点填站点配置里的列表入口网址，接口站点填 `records`，PubMed 填检索词，`fda_crl` 填 `*`），**不要在配置里写 `known_stop`**，每一轮由 `collection.windows.cycle_request` 按目录填。读法是老 ETL 的“增量爬取：遇到第一个重复的就停止”：每轮从列表第一页往后读，每读完一条，问一次库里这条记录的当前版本是不是和它一样（`db.history.stored_unchanged`，和存版本判断“新版本还是没变”的是同一段代码），连续 3 条（`collection.windows.KNOWN_STOP`）都是，就以 `reached_known` 停：前面的都读过了，后面更旧。已存且没变的父记录不再下载它的附件（上一轮存过）。这个办法的前提是列表从新到旧、旧条目不会被改动：读不到被改过的旧条目；列表顺序是站点的默认顺序，不是来源的承诺（openFDA 召回、说明书读取时明确要求按日期从新到旧；Fierce Pharma 把两个 RSS 并成一个从新到旧的队列）。一个周期最多读 5 段（`collection.windows.SEGMENTS_PER_CYCLE`，每段最多 `max_records` 条）就收工、记成功：第一次读（库里什么都没有）也只读最新的这些，更老的用 `ops/backfill_source.py` 补，不会一个周期接一个周期地读下去。一段读完、续读位置没变、又一个版本都没读到（比如接口站点重放就用完了截止时间），记失败 `source_watch_no_progress`，按 `cadence_seconds` 再试，不立刻重发；同一周期连续失败 3 次就放弃这一周期，下个间隔从头重新开始。
`fda_crl` 也能进固定刷新，但不用这个办法：FDA 成批公布信函，旧日期的信可能排在已读过的新信后面，“遇到已存的就停”会漏掉它们，所以它每个周期整份重读（目前约 460 封，5 次请求），没变的不重复存版本（目录里的增量键是 `full_reread`，`known_stop` 恒为 0）。
用不了这个办法的来源不进固定刷新，原因写在目录里（`collection/catalog.py` 的 `_NOT_WATCHED`）：`hkex`（183 家公司各自从新到旧，停在第一家就漏掉其余，而且一轮要约 6 分钟，一次读取最长 120 秒，续读又要从头重放）、`nccn`（清单没有时间顺序，新版本只是某一条的版本号变了，必须整份重读，一轮约一百次页面渲染）、`drugbank`（没有 API 凭据与许可）。
读不到的条目不存版本（读取结果的 `unread_items` 里有网址和原因）：列表站点里一个详情页读不出来，和接口站点的 `ItemUnreadable` 一样，记下它、接着读下一条，读到底的那一段标成“部分完成”（`items_unread`）；连续 5 条（`collection.site_crawler.SITE_DOWN_AFTER`）都读不出来则不再往下读，整次读取失败，原因写“站点可能出了故障”。没存的下一轮自然会再碰到，但“遇到已存的就停”可能停在它前面，所以：**一个周期里任何一段有条目读不到，周期收尾时 watch 的状态里记 `read_all`，下一个周期从头读、不带 `known_stop`**（`db.source_refresh`；仍受一个周期最多 5 段的限制），读干净了再用回 `known_stop`。整次失败的读取照常按 `cadence_seconds` 重试，重试的那一段也不带 `known_stop`（`_read_all_next_time`）。同一条连续 3 轮（同一个读取入口，从第一段起的一次整读算一轮）都读不到，`GET /api/history/source-health` 的 `stuck_items` 里单列出来（网址、原因、轮数），空表示没有；进了这张表的（死链）不再触发整份重读。数据就是各次读取留在 coverage 里的 `unread_items`，没有另建表。
列表页一条都没解析到（列表页改版、接口改了格式的样子）不算读完：读取标为失败，原因 `list_empty`“列表页一条都没解析到，可能改版”。和“没有新内容”分得开：后者是解析到了、但全是已存的，读取正常结束（`reached_known`）。多页的列表翻到某一页一条都没有，就当列表到头了、不再往后翻（再翻只会得到更多空页或 404）；第一页就没有才是上面说的失败。Fierce Pharma 的某个 RSS、NCCN 的某个分类列表读不到，也不再只记日志：RSS 记成读不到的条目（别的 RSS 照读），NCCN 列表渲染失败让整次读取失败。

请求次数不设额度：读取只受“抓几条”（`max_records`）、截止时间（`deadline_seconds`）、字节数、对方网站的间隔和本机的出站上限（`collection.rate`）约束；请求数照实计数写进读取结果（`requests`），`max_pages` 只管接口分页读的页数，不再折成请求数上限。

GDELT 线索通道（线索不进 WeKnora）：`GET /api/history/records` 对 `gdelt_doc` 可加 `window_start`、`window_end`（含时区，半开区间 [起, 止)），按 `source_updated_at`（含义 `gdelt_first_observed`，GDELT 的观察时间）列出落在窗口里的线索；每条是固定版本，带 `title`、`source_url`、`facts.domain`。同一响应的 `coverage.window_coverage` 和 `coverage.truncated_intervals` 是这个窗口的读取回执：没给 `run_id` 时取截止时间前已结束、读取范围与窗口相交的该来源读取运行的回执（同一查询的同一区间以最后一次运行为准），给了 `run_id` 只取那一次运行的。一条线索所属的窗口是 `status=read` 且 [start, end) 含它观察时间的那条回执；`possibly_truncated` 的区间里可能还有读不到的线索，`failed`、`not_read` 的区间没有读到。窗口里的回执超过 2000 条或读取运行超过 200 次时拒绝（`window_too_wide`），请缩小窗口。

出站额度是本实例自己的硬限，不是与其他实例或现役系统共享的全局限：GDELT 间隔 6 秒（对方要求至少 5 秒；收到 429 就按对方说的间隔等一次、再发一次，仍是 429 才停），每个查询每小时 ≤7 次、每天 ≤900 次；who.int 间隔 30 秒，每天 ≤200 次（回填的请求也算在内）。额度写在 `collection/catalog.py` 的 `outbound_cap`，计数放在限速闸门的同一处（配置了 `DATAFABRIC_HISTORY_REDIS_URL` 用 Redis，否则是 `DATAFABRIC_RATE_DIR` 下的文件），跨读取、跨进程累计，按 UTC 小时和 UTC 日算。到限的请求不发出，读取停下，`stop_reason` 和未读窗口的回执写 `outbound_quota_hour` 或 `outbound_quota_day`。Redis 被清空或限速目录被删，计数从零开始。

WHO 的固定刷新每 15 分钟一轮：目录给 who_don 声明的刷新间隔下限是 900 秒（其他来源仍是 3600 秒），窗口重叠 2 天、检查点照旧。一轮至少一次请求，每天 96 次起，在 who.int 每天 200 次之内。读取碰到出站额度时，这一轮带着原因结束，不算失败：`last_coverage.stop_reason` 写 `outbound_quota_day`，状态回到 idle，已读的页保留，检查点不动，也不记 `last_success_at`；下一轮（`cadence_seconds` 之后）从续页位置接着读，一页都没读到时按同一检查点重新规划窗口。其他没有续页的提前停止仍是 `source_watch_no_cursor`。

WHO 有界回填是显式触发的一次性操作，不在 `DATAFABRIC_HISTORY_WATCHES` 里：`python -m flows.who_backfill --until <冻结时刻，带时区的 ISO 8601>`（可选 `--since <起点>` 换起点，默认 2024-01-01；另一个起点是另一次任务，运行键里带起点，旧任务及其回执不动）。它在本进程内读（不经 RQ），用与 Worker 相同的数据库和限速配置。读 `LastModified ≥ 2024-01-01` 到 `--until` 的公告，按 LastModified 升序、每条只取当前版本，碰到任一上限就停：400 条、40 MiB（请求次数没有自己的上限，至多 8 次，计在当日 200 次之内）。每一段是一个读取运行（插件每页 50 条，一个运行最多 100 条）。运行的 coverage 里 `backfill` 是回执：上限、已用量、停止原因（`records_bound`、`bytes_bound`、`reached_end`，或出站额度、读取失败）、下一段起点的 LastModified/Id 游标和“回填有界，未到底”。`backfill.background` 为真，`backfill.background_version_ids` 是这个运行新建的版本：它们只作背景，不是新信息，不计入活动指标、活动基线和“首次见到”；回填读到、但此前已被别的运行采用的版本不在其中。同一个 `--until` 再运行会重放已完成的各段（不发请求）并接着往下做，上限跨次累计；上一段仍在运行时报 `backfill_leg_unresolved`，先处理那个任务。字节上限按传输层的计量：越过上限的那一页整页丢弃，计数可能多出越线的那一块。

测量有两条候选路线，同一个入口（`python -m flows.measure_extract`，处理器 `datafabric.measure_extract.v5`）：模型路线（配了 `DATAFABRIC_MEASURE_LLM_*` 才有）和规则路线（`who.explicit-count.v1` 的明确计数，不调模型、不发请求；`DATAFABRIC_MEASURE_RULES_ROUTE=false` 关闭）。两路的读数进同一批表，经同样的严格请求、口径和 BioCheck 核验：核验只给对象打标记（`check.status`：`consistent` 已核一致 / `inconsistent` 有冲突 / `unchecked` 未核），不决定有没有对象，读数读出（数值、逐字定位、时期形式成立）核验任务一开始就是未核的对象，核验回答后由回答的对象取代；路线写在生产者标识里（规则路线是 `datafabric.measure_extract.v5+rules.who.explicit-count.v1`），一条路线只替代自己的旧运行。两路读的是同一段原文：`/measure-coverage` 每个版本的 `route_agreement` 按 9 项（对象、指标、单位、时期、地区或人群、累计或新增、疑似或确诊、条件、限定词）比对，值落在同一位置的两个读数 9 项都一致才记 `agree`，否则两条都留、记 `conflict`，只有一路读到的单列；`independent_evidence` 恒为 `false`，一致也不是两份独立证据。

常驻队列不计划有界回填新建的版本（回执里的 `background_version_ids`）：补产 v12 不会让它们隐式吃掉测量。要测它们，给一份清单：`python -m flows.measure_extract --scope list.json`，清单是 `{"versions": ["<版本号>", {"version_id": "<版本号>", "windows": [0, 3]}]}`。只计划、只读清单里的版本；写了 `windows` 的只读这些阅读窗口，其余窗口存为 `skipped`（`out_of_scope`），不算欠账、不事后删。清单摘要和所选窗口写在每个运行的 `selection` 上（V041，常驻队列的运行为空），`/measure-coverage` 带出它和 `out_of_scope`；打印的回执写明计划了什么、哪些版本已有运行、哪些没计划成及原因（例如清洗正文 v12 还没产出、窗口号不存在）。窗口号按生成这个运行的处理器版本的切法计：v2 起第 0 个窗口也在行或句子的边界结束，同一个窗口号在 v1 与 v2 下对应的原文范围可能不同。一次调用最多 20 轮，每轮模型请求数仍受 `DATAFABRIC_MEASURE_ROUND_REQUESTS` 约束，窗口失败仍是 3 次封顶，回执记本次发出的请求数。同一产物同一路线已有运行的版本不再计划；加 `--upgrade` 则只认当前 producer（处理器版本和模型配置）的运行：旧 producer 读过的版本另起新运行，旧运行原样保留，清单以外的版本不动。`--upgrade` 只能和 `--scope` 一起用。

新增身份结构使用 `V035__identity_evidence.sql`（团队分支上的原编号是 V023，合并时改号，原因见 docs/decisions/0001）。已按团队编号迁移过的库直接运行 `python -m migration.migrate` 即可：迁移器按版本号、描述和校验和认出改号的迁移，原记录不删不改；认不出的记录会报错并停止（先用 `--list` 只读查看）。测试初始化顺序是：对象库 `python -m migration.migrate`、独立历史库 `python -m migration.history`（索引表也在这一步装好）。发表库如果就是对象库，`migrate` 已经装了 V019 和 V026，不再另跑；只有发表库是另一个没跑过 app 角色的原采集库时，才多一步 `python -m migration.runner --db publication --apply`。先注入并核实测试环境配置，再执行；迁移命令本身不负责选择安全的数据库。回退代码时保留新表和历史数据，不把新产生的数据覆盖成旧备份。

历史演练如果只准使用已经归档的材料，设置 DATAFABRIC_HISTORY_FETCH_ENABLED=false，同时不启采集 Worker；API 和实际公开读取都会拒绝外采。DATAFABRIC_HISTORY_WATCHES=[] 关闭自动刷新。不开索引时使用空 DATAFABRIC_INDEX_TARGETS，且不启发布循环；既有索引的撤回仍应按原管理流程核对，不能把删配置当已撤回。

仅做模拟资料归档与检索联调时，archives 请求同步落入 History；随后用同一隔离配置运行 python -m flows.scheduler --poll-seconds 5 --batch-size 5，由后台自动建索引并核对回执。归档不需要另造导入队列。结束时给测试发布进程发 SIGTERM，保留持久待办，不能清空队列或数据库。

## 隔离补采与字段交接（2026-10-31）

雷达的模拟任务可以向**专门配置的测试 Fabric** 发起补采。仍使用
`POST /api/history/fetch` 和原来的 RQ 队列、History 版本保存流程。
这条测试路径从服务器上的待采原件包取资料，不联网，不向现役库写入。
返回了任务号不等于补采完成：须继续读取 `GET /api/history/runs/{id}`，
核对状态、`version_ids` 和 `coverage`。

请求沿用原字段，增加：

- `is_demo: true`；
- `demo_instance_id`：与测试 Fabric 约定的实例编号；
- 模拟材料可用 `source_id: "external_archive"`。真实补采仍使用原来的公开来源，
  默认 `is_demo: false`，不得带模拟实例编号。

测试进程必须显式设置 `DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG`。下面是格式示例，
端口、库名和文件路径要换成该次新建资源的实际值：

```json
{
  "instance_id": "fabric-demo-example",
  "database": {"host": "127.0.0.1", "port": 25432, "name": "fabric_demo_test"},
  "redis": {"host": "127.0.0.1", "port": 26379, "db": 0},
  "input_file": "/absolute/private/demo-inputs.json"
}
```

程序核对上述地址与本进程的 History 数据库、Redis 配置一致，并限制为回环地址。
没有这项配置、实例号不符或后端地址不符时拒绝模拟补采。现役进程不要设置它。
读写凭据仍使用已有 History 配置，不能写进请求样例或 Git。
`DATAFABRIC_HISTORY_FETCH_ENABLED=false` 可以关闭外部公开读取；隔离模拟补采另按上述条件检查。

待采包格式如下，`sources` 内每条记录直接使用现有 `ArchiveRecord` 格式：

```json
{
  "schema_version": "datafabric.demo_fetch.v1",
  "instance_id": "fabric-demo-example",
  "sources": {
    "external_archive": [
      {
        "source_record_id": "SYNTHETIC-EXAMPLE",
        "record_kind": "document",
        "raw_media_type": "application/json",
        "raw_content": "{\"record_kind\":\"document\",\"title\":\"SYNTHETIC example\",\"full_text\":\"Synthetic test text.\"}"
      }
    ]
  }
}
```

包内只放本阶段已经可见、允许补采的材料，不放后续波次或裁判答案。
`query: "*"` 表示在这个私有包内按数量上限取资料；其他查询按空格分词，
要求每个词都出现在原站编号、标题或正文中。可用 `source_record_ids` 限定编号。
这是模拟来源的字面查询，不代表公开站点的搜索能力。
不支持模拟时间窗和采集续页，传入这些参数会明确报错。

接受任务时会保存本次选中的原件，Worker 和恢复任务读取这个固定输入。
同一幂等键仍指向原任务；输入包后来发生变化，不会改掉已接受任务的材料。
回执明确返回 `fetch_mode: "simulated"`、`demo_instance_id`、`input_sha256`，
以及实际保存的版本。查询没有命中时说明 `demo_query_no_match`；超过条数上限时说明
`record_budget`，不能把它当成完整覆盖。模拟补采不会自行生成历史可见时间证明。
要做历史回放，仍通过 `/archives` 提交原件、许可和可得时间依据。

普通 `external_archive` 文档现在保留 `company_name`、`period_start`、
`period_end`、`unit`、`metric_role`、`value`、`license`、`license_url`，
并为存在的字段附上原件 JSON 位置。字段不存在、显式空值和数值零分别保留。
这些只是来源填写的原值：不据公司名确认身份，不据单位和角色名称认定可比，
也不据许可字符串认定已经获准使用。

需要明确数值口径时，沿用 `datafabric.observation.v1` 或 SEC 的
`financial_observations`。更正继续使用 `document_relations`，有旧版回执时绑定
`target_version_id`；删帖、辟谣等继续保留在 `information_events`，不自动判断真假。
图查询是另一条已有能力：材料补采不会生成已核图事实。任务没有包含图来源时应说明
`graph_source_not_in_scope`；需要查询模拟图时，原文先归档，节点与关系保持实际的待核状态，
再按已有图接口查询，不能为通过验收填成人工审核通过。

本次还补了环境备份的忽略规则。`.env.bak` 等真实配置不应提交；
`.env.example` 仍可正常跟踪。现役目录的本地启动脚本和虚拟环境另由本地
`.git/info/exclude` 保护，不移动或覆盖学生文件。

本次没有数据库迁移。部署或回退时，API 与 Worker 使用同一版本；先停收新的模拟任务，
已接受任务应由相应版本处理完或保留待恢复。不要用清队列、覆盖数据库来回退代码。


## 迁移执行器与来源能力（r2）

迁移统一走 `python -m migration.runner --db {app|history|publication}`；旧命令（`migration.migrate`、`migration.history`）是它的包装，输出和退出码语义不变。`migration.publication` 这个包装已经删除，
原采集库的迁移和站点索引直接用 `--db publication`；`migration.index_publication` 只剩 `worker`，
索引表在 history 角色里，迁移走 `--db history`。

```
python -m migration.runner --db history --plan              # 只读：已记录什么、待执行什么、为什么拒绝；不建任何表
python -m migration.runner --db history --apply             # 执行待办（整文件、每文件一个事务、同事务写账本）
python -m migration.runner --db history --adopt-baseline    # 没有账本的旧库：先证明结构，再建账（observed_baseline）
python -m migration.runner --db history --revoke-baseline V020 --reason ...   # 追加一条更正事件
python -m migration.runner --db publication --apply          # 只装 V019、V026；什么时候需要见下
python -m migration.runner --db publication --online site-index [--drop]     # CONCURRENTLY 操作
python -m migration.source_lifecycle effective legacy_documents --actor NAME  # 退役在本次部署生效（写部署时刻）
```

**三个角色什么时候跑。** `app` 是对象库（V001–V035 兼容清单，其中已有 V019 的 `publication_outbox` 和写入触发器、V026 的加列）；`history` 是独立历史库；`publication` 只装 V019 和 V026，给一个没跑过 `app` 角色的原采集库用。
D1 和普通部署只跑 `app` 和 `history`：发表器读的库就是 `app` 建过的那个库时，V019 已经在里面，不要再跑 `publication`。在这样的库上跑 `publication`，runner 会拒绝（`publication_outbox` 已经存在，账本里却没有 publication 角色的记录）。这是入口选错，不是业务数据出了问题：这个库不需要 publication 角色，不用为它建账。
只有发表库是另一个、没跑过 `app` 的原采集库，才用同一个 runner 跑一次 `--db publication --apply`（`DATAFABRIC_PUBLICATION_DATABASE_URL` 必须显式给出，不读默认 APP 库）；建按来源补录索引用 `--db publication --online site-index`。这个库如果早先已用旧的单独命令迁移过、还没有账本，先用 `--db publication --adopt-baseline` 建账，不要重跑文件。

现役库第一次：先 `--plan`（会提示“库在账本之前已迁移”），再 `--adopt-baseline`（会在一次性容器里取各前缀的结构指纹，
按记录的容器 ID 拆除），结构不等于任何前缀就只出差异报告，不写任何东西；没有 `--accept-diff`。设计和限制见
`docs/decisions/0004-migration-runner.md`。

`GET /api/history/sources` 另外给出 `capabilities_version`、`definition_sha256`、`receipt_sha256`、`retired_sources`、
`unavailable_sources`，每个来源带 `capabilities`；说明见 `docs/decisions/0005-source-catalog-and-retirement.md`。
`publication/status` 的 `source_id` 必填。

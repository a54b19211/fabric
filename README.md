# ZK_Data Fabric — 数据基座 ETL

面向药学领域的数据采集与处理流水线，支持网页爬取、本地 PDF 批量解析、AI 结构化提取、质量门控与人工审核回灌。

## 版本使用

本地开发和运行统一使用 `dev`。已验证的任务保护 `4e7ba15` 与研究读取 `9ab8bd4` 已归入主线，临时分支已删除；其他学生分支保留，本轮未推送远端。正常修改按任务提交，不按版本或日期另开交付分支。回退查提交记录和已有标签，不把旧版另留成一套主代码。

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

### 完整流水线（爬取 + 本地 PDF + 导出审核）

```bash
python main_etl.py
```

### 仅爬取网页

```bash
python main_etl.py --mode crawl
```

### 仅处理本地 PDF

```bash
python main_etl.py --mode local
```

### 仅导出待审核 CSV

```bash
python main_etl.py --mode export_review
```

### 人工审核回灌

1. 运行 `export_review` 导出待审核 CSV
2. 用 Excel 打开 CSV，填写 `manual_score` 和 `reviewer_note` 列
3. 保存后执行回灌：

```bash
python feedback_ingest.py --csv /data/review/pending_review_20240315.csv
```

---

## 项目结构

```
├── main_etl.py              # ETL 主入口（Prefect 调度）
├── feedback_ingest.py       # 人工审核回灌脚本
├── requirements.txt         # 依赖清单
├── .env.example             # 环境变量模板
├── config/
│   ├── settings.py          # 全局配置（API Key、路径、阈值）
│   └── crawler_settings.py  # 爬虫站点配置
├── core_engine/             # 核心引擎（采集、解析、质检、存储）
├── biz_rules/               # 业务规则（清洗、提取、Schema）
└── logs/                    # 运行日志（按日期滚动）
```

---

## 数据输出目录（DATA_ROOT 下）

| 目录 | 内容 |
|---|---|
| `raw/` | 原始文件（HTML、PDF） |
| `markdown/` | 解析后的 Markdown 全文 |
| `json_envelopes/` | 结构化 JSON 元数据信封 |
| `quarantine/` | 质量不达标的隔离数据 |
| `review/` | 待人工审核的 CSV 导出 |

---

## 新增爬取站点

编辑 `config/crawler_settings.py`，在 `SITE_PROFILES` 字典中追加条目即可：

```python
"your_site_key": {
    "name": "站点名称",
    "url_pattern": "example.com",
    "start_urls": ["https://www.example.com/news/"],
    "use_css_extractor": False,
    "request_delay_seconds": 2.0,
    "headers": DEFAULT_HEADERS,
    "max_pages": 30,
},
```

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

旧网页/CLI链尚未统一为历史版本发布：同UUID变化正文拒写并保留旧成果；API来源的旧URL重采明确拒绝，不能先清空唐的抽取/图谱/同步结果。
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

普通测试套件 247 项通过；最后时间歧义修正后，相关 16 项单元测试和 12 项真实 PostgreSQL 测试通过。这些数量存在重叠，不累加成一个更大的测试总数。跨服务最终 42 项通过，雷达还完成实际浏览器操作和跨旧入口的撤源检查。完整联调记录集中在同机雷达分支的 `evaluation/handoff-verification.json`。

当前只提交联调分支，没有切换现役 dev、重启现役服务或改其数据库。临时测试进程已关闭，测试数据库保留；现役启动保护仍在。后续合入时先加载明确的历史配置并执行新增迁移。撤回代码时停止新任务、保留历史表和原件，不以删表或清队列回退。

# ZK_Data Fabric — 数据基座 ETL

面向药学领域的数据采集与处理流水线，支持网页爬取、本地 PDF 批量解析、AI 结构化提取、质量门控与人工审核回灌。

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

本次修复基于 `9bb63b89`，分支为 `fix/zktest-task-safety`。保留原来的
`dev` 分支和服务器配置，不改数据库结构，不重跑历史资料。

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
git switch -c revert/zktest-task-safety release/zktest-task-safety-20261016
git revert release/zktest-task-safety-20261016
```

**不要在仍运行着 `uvicorn --reload` 的目录直接执行回退。** 原版本启动会
清队列，首次查询状态还会重发在途任务。运行服务要回退时，先停止新任务受理，
完整停止 API 的 reload 父子进程和 Worker，核清 Redis 排队/在途任务及数据库
queued/processing 记录。确认没有未处理任务后，才能切回基线并按原方式启动。
无法确认时保留保护修复，先处理任务，不直接启动旧版本。

Git 撤销的是代码。本次不需要恢复旧数据库，也不能用旧数据库或 Redis 快照
覆盖新产生的业务数据。配置、数据和未跟踪文件保持原样；不使用强制重置或清理命令。

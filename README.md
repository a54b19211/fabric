# ZK_Data Fabric：抓 → 洗 → 存

把药学相关的公开资料（监管公告、临床登记、文献、企业披露、新闻……）**抓**下来，**洗**成干净正文、编号和画像，
按版本**存**进 History 库并送进知识库 WeKnora。雷达只经 HTTP 接口（`api/`）读这些结果。

- **正式运行**：`systemctl --user start 'datafabric-*'`（`ops/systemd/` 的 7 个服务，各起 `run/` 里的一个常驻程序，见下）
- **调试一个来源**：`python main_etl.py --source nmpa --max 5`（会写库，先指向临时库，见“怎么跑”）
- **跑测试**：`make test`

## 目录

| 目录 | 做什么 |
|---|---|
| `main_etl.py` | 一个来源从抓到存的全过程，从上往下一步一个函数；调试和学习从这里读起 |
| `fetch/` 抓 | 来源目录 `catalog.py`、每类来源的读法 `sources.py`、网站读取 `site_crawler.py`、站点配置 `sites/`、详情页提取 `extractors/`、采集任务 `fetch_job.py`、定时刷新 `source_refresh.py` |
| `clean/` 洗 | 正文 `clean_text.py`、编号 `identifiers.py`、画像 `profile.py`，由 `clean_stage.py` 按版本批量做；附件解析、登记差分、测量读数、BioCheck 核验 |
| `store/` 存 | History 库：版本和运行 `history.py`、数据契约 `history_models.py`、送知识库 `index_publication.py`、检索 `retrieval.py`、迁移 `migrations/` |
| `api/` 对外 | HTTP 接口；雷达读的全在 `api/routers/history.py`（`/api/history/*`） |
| `run/` 运行 | 常驻程序的入口：派活 `scheduler.py`、清洗 `clean.py`、附件 `attachments.py`、索引 `index.py`、测量 `measure.py`、核验 `checks.py` |
| `graph/` 图谱 | 对象库、模型抽取、消歧、人工审核、图发布和审核页面（唐组维护，徐组不用读） |
| `ops/` 运维 | 体检 `check_sources.py`、补抓存量 `backfill_source.py`、服务文件 `systemd/`、代码门槛 |
| `tests/` | 测试 |

## 怎么跑

环境：CPython 3.10、uv 0.13.0。`make venv` 只按 `requirements.lock` 装（契约包和模型传输包从旁边的
`../extraction-contract/dist`、`../llm-transport/dist` 取，先在那两个检出里构建）。

调试一个来源（`main_etl.py`，会写库，一定指向临时库）：

```bash
export DATAFABRIC_HISTORY_DATABASE_URL=postgresql://…/临时库
python -m store.migrations.runner --db history --apply    # 建表
python main_etl.py --source nmpa --max 5                  # 抓 5 条 → 清洗 → 测量 → 送知识库（没配就跳过）
python main_etl.py --source clinicaltrials                # 来源目录里能抓的来源都行
```

正式运行：`ops/systemd/*.service` 装到 `~/.config/systemd/user/`，环境变量放 `~/.config/datafabric-publication/runtime.env`：

| 服务 | 启动的程序 | 做什么 |
|---|---|---|
| datafabric-publisher | `python -m run.scheduler` | 到点给各来源建采集任务，放进队列 |
| datafabric-history-worker | `rq worker … extraction` | 执行采集任务（`fetch/fetch_job.py`），原件存成版本 |
| datafabric-clean | `python -m run.clean --loop` | 新版本的正文、编号、画像 |
| datafabric-index | `python -m run.index worker` | 清洗正文送进 WeKnora |
| datafabric-measure | `python -m run.measure` | 从正文读数字 |
| datafabric-checks | `python -m run.checks` | BioCheck 核对编号、画像、读数 |
| datafabric-history | `uvicorn api.main:app` | HTTP 接口 |

附件正文解析 `python -m run.attachments --loop` 现在没有服务文件，要用时手动起。

主要环境变量：`DATAFABRIC_HISTORY_DATABASE_URL`（History 库）、`DATAFABRIC_HISTORY_SOURCE_ALLOWLIST`（允许读的来源）、
`DATAFABRIC_HISTORY_WATCHES`（定时刷新哪些来源）、`DATAFABRIC_HISTORY_REDIS_URL`（采集队列）、`DATAFABRIC_INDEX_TARGETS`、
`WEKNORA_KDD_URL`、`WEKNORA_KDD_TOKEN`（知识库）、`DATAFABRIC_BIOCHECK_URL`、`DATAFABRIC_BIOCHECK_TOKEN`（核验）。
NCCN 走代理：设 `DATAFABRIC_PROXY_NCCN=http://127.0.0.1:7890`（zktest 上的 mihomo），nccn 的页面渲染和 PDF 下载都经它；
不设就直连。只有站点配置里写了 `proxy_env` 的站点（现在只有 nccn）用代理，不设全局代理。

测试和检查：`make test`（单元测试，不连库）、`make lint`、`make metrics`（函数 80 行、复杂度 20 的门槛）、
`make test-integration`（起临时 PostgreSQL/Redis 容器，跑完删掉）、`make check-lock`（要连 PyPI；zktest 上临时加
`HTTPS_PROXY=http://127.0.0.1:7890`）。

## 读代码的顺序

1. `main_etl.py`：整条链一页读完，每一步调用的是哪个文件的哪个函数。
2. 抓：`fetch/catalog.py`（有哪些来源）→ `fetch/fetch_job.py` 的 `history_fetch_job` → `fetch/sources.py` 的
   `read_public` → 网站走 `fetch/site_crawler.py`，站点知识在 `fetch/sites/<站点>.py` 和 `fetch/extractors/`。
3. 存版本：`store/history.py` 的 `finish_fetch`（原件只追加，内容没变不另存）。
4. 洗：`clean/clean_stage.py` 的 `process` → `clean_text.py`、`identifiers.py`、`profile.py`。
5. 送知识库和对外：`store/index_publication.py`，`api/routers/history.py`。

一份资料经过哪些文件、函数和表，见 [docs/代码地图.md](docs/代码地图.md)。

**学生不用读 `tests/` 和 `fetch/transport.py`（还有 `fetch/rate.py`）**：每个对外请求都会自动经过它们，排队限速、
记预算；写采集代码时不要自己 `sleep` 限速，也不要自己数请求。

## 怎么加一个站点

1. 在 `fetch/sites/` 加一个文件，写 `SITE_KEY` 和 `CONFIG`：列表入口 `start_urls`、列表和详情的选择器、翻页方式；
   接口站点写 `"source_type": "api"` 和拉取函数 `api_fetcher`。照着最像的一个站点抄（网页站点看 `nmpa.py`，接口看 `fda/`）。
2. 通用解析读不好详情页时，在 `fetch/extractors/` 写一个提取函数，配置里 `detail_extractor` 指向它。
3. 在 `fetch/catalog.py` 加一行 `_site(...)`：来源编号、名称、证据类型、限速组和间隔、覆盖说明；`verified` 先写 False。
   默认按“列表从新到旧、遇到已存的就停”刷新；列表不是从新到旧的，写 `incremental=_FULL_REREAD`（每轮整份重读）。
4. 跑 `make test`：`tests/test_source_catalog_golden.py` 会指出 `tests/fixtures/source_catalog_golden.json` 少的许可项，看过再补进去。
5. 让库认识它：临时库上 `python -m store.migrations.runner --db history --apply`（补一行 `history_sources`），
   allowlist 里加上它；要定时刷新的写进 `DATAFABRIC_HISTORY_WATCHES`。
6. 真抓：`python main_etl.py --source <站点> --max 5`，再 `python ops/check_sources.py <站点> --rounds 3 --incremental`
   （第二、三轮应停在 reached_known，不重复存版本）；照 `tests/test_site_pages.py` 存一页夹具，补一个提取测试。

## 其他

- 徐子雯代码的对接方案：[docs/徐子雯对接方案.html](docs/徐子雯对接方案.html)
- 设计、决定和交接记录在 `/home/zk_test/zk_projects/雷达设计文档/`，代码目录里只放代码。

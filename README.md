# ZK_Data Fabric：抓 → 洗 → 存

把药学相关的公开资料（监管公告、临床登记、文献、企业披露、新闻……）**抓**下来，**洗**成干净正文、编号、画像和质量分，
按版本**存**进资料库并送进知识库 WeKnora。雷达只经 HTTP 接口（`api/`）读这些结果。

- **正式运行**：`systemctl --user start datafabric-{scheduler,fetch-worker,clean,index,api}`（`ops/systemd/` 的服务，各起一个常驻程序，见下）
- **调试一个来源**：`python main_etl.py --source nmpa --max 5`（会写库，先指向临时库，见“怎么跑”）；**跑测试**：`make test`

## 目录

| 目录 | 做什么 |
|---|---|
| `main_etl.py` | 一个来源从抓到存的全过程，从上往下一步一个函数；调试和学习从这里读起 |
| `fetch/` 抓 | 来源目录 `catalog.py`、读法 `sources.py`、原件投影 `projection.py`、网站读取 `site_crawler.py`、站点配置 `sites/`、详情页提取 `extractors/`、出网 `transport.py`、采集任务 `fetch_job.py`、定时刷新 `source_refresh.py` |
| `clean/` 洗 | 正文 `clean_text.py`、编号 `identifiers.py`、画像 `profile.py`、登记差分 `registry_changes.py`，由 `clean_stage.py` 按版本批量做；附件解析 |
| `store/` 存 | 资料库：存版本和读版本 `records.py`、数据形状 `record_models.py`、送知识库 `index_publication.py`、检索 `retrieval.py`、迁移 `migrations/` |
| `api/` 对外 | 雷达读的 HTTP 接口：`api/routers/fabric.py`（`/api/fabric/*`） |
| `run/` 运行 | 常驻程序的入口：派活 `scheduler.py`、清洗 `clean.py`、附件 `attachments.py`、索引 `index.py` |
| `weknora/` 桥接 | WeKnora 桥接服务（独立进程 18564，`ops/systemd/datafabric-weknora.service`）：上传、停用、特征、检索、重排，见代码地图 |
| `ops/` 运维 | 体检 `check_sources.py`、补抓存量 `backfill_source.py`、补指纹 `backfill_fingerprint.py`、服务文件 `systemd/`、代码门槛 |

谁调谁：`run/`、`api/` → `fetch/`、`clean/` → `store/`。存（`store/records.py`）不调读取和清洗，只用来源目录、投影和数据形状
这些不连库、不联网的东西。图谱（抽取、消歧、审核、图发布）是另一个项目 Atlas，不在这里；资料库里旧的图包版本照常能读，不再能导入。

## 版本怎么定（`store/records.py` 的 `fingerprint`、`_publish`）

每条原件算一个内容指纹：JSON 按键排序重写，去掉 ClinicalTrials 每次都改的 `versionHolder`，网页抓取不看 HTML
（只看解析出的标题、正文和附件清单），附件不看本机落盘路径 `store_path`；不是 JSON 的原样算。和这条记录的当前版本
（`replaced_by` 为空的那个）比：相同就不存；不同就存一个新版本，旧版本的 `replaced_by` 指向它。原件只追加，A→B→A 是第三个版本。
附件文件按 sha256 去重；定时刷新连续读到 3 条“已存且没变”就停。

## 质量等级（`clean/profile.py` · `quality_level`）

画像的 `quality` 是格式、来源可信度、实体匹配、事实核验四项状态，`route` 是分流。`/clean-text?quality=true` 把它折成一个等级给 Atlas：
`route=quarantine` → 隔离，`weak_signal` → 低；`normal` 里格式 ok 且来源可信度已知 → 高，其余 → 中。

## 怎么跑

环境：CPython 3.12、uv 0.13.0；`make venv` 只按 `requirements.lock` 装。

调试一个来源（`main_etl.py`，会写库，一定指向临时库）：
```bash
export DATAFABRIC_DATABASE_URL=postgresql://…/临时库
python -m store.migrations.runner --db fabric --apply    # 建表（--plan 只列出还没跑的迁移）
python main_etl.py --source nmpa --max 5                  # 抓 5 条 → 清洗 → 打分（只处理这次抓到的）
python main_etl.py --only index                           # 送知识库处理全库到期的活，要 --only 点名
```

正式运行：`ops/systemd/*.service` 装到 `~/.config/systemd/user/`，环境变量放 `~/.config/datafabric-publication/runtime.env`。
服务文件里的路径是服务器上的部署检出 `/data/projects/fabric` 和它的 `.venv`；共用基础设施（PostgreSQL、Redis、WeKnora 等）在部署仓库 a54b19211/ZK-radar-deploy。开发时用本仓的 `.venv`。

| 服务 | 启动的程序 | 做什么 |
|---|---|---|
| datafabric-scheduler | `python -m run.scheduler` | 到点给各来源建采集任务，放进队列 |
| datafabric-fetch-worker | `rq worker … extraction` | 执行采集任务（`fetch/fetch_job.py`），原件存成版本 |
| datafabric-clean | `python -m run.clean --loop` | 新版本的正文、编号、画像、登记差分；之后给画像过的版本打质量分（每轮限时 60 秒，到点做完手上那个版本才停） |
| datafabric-index | `python -m run.index` | 清洗正文送进 WeKnora |
| datafabric-api | `uvicorn api.main:app` | HTTP 接口 |

附件正文解析 `python -m run.attachments --loop` 没有服务文件，要用时手动起。环境变量：
- 抓、洗、存都要：`DATAFABRIC_DATABASE_URL`（资料库）；定时刷新再要 `DATAFABRIC_REDIS_URL`（采集队列）、
  `DATAFABRIC_WATCHES`（刷新哪些来源，进度存在资料库的 `fabric_source_watches`）。
- 接口：`DATAFABRIC_READ_TOKEN` / `DATAFABRIC_ADMIN_TOKEN`；知识库：`DATAFABRIC_INDEX_TARGETS`、`WEKNORA_KDD_URL`、`WEKNORA_KDD_TOKEN`。
- 可选：`DATAFABRIC_PROXY_NCCN=http://127.0.0.1:7890`（NCCN 走服务器上的 mihomo，
  只有站点配置写了 `proxy_env` 的站点用代理）；`DATAFABRIC_FETCH_ENABLED=false`（这个实例不出网，只读已存的版本）；
  磁盘余量 `DATAFABRIC_PUBLICATION_DISK_PATH`（默认当前目录）低于 `DATAFABRIC_PUBLICATION_MIN_FREE_BYTES`（默认 15 GB）时不建新的采集任务。

来源开关在库里：`fabric_sources.read_enabled`（能不能读）、`fetch_enabled`（定时刷新抓不抓），管理员用 `GET /api/fabric/admin/sources` 看、
`PATCH /api/fabric/admin/sources/{source_id}`（`{"read_enabled":..,"fetch_enabled":..,"changed_by":..}`）改，每次改记进 `fabric_source_switch_log`。升级旧库：V042 加 `fingerprint`、`replaced_by`（再跑 `ops/backfill_fingerprint.py`
补旧指纹，不补也能用）；V043、V044 质量分表和对齐表；V045 `fabric_source_watches`（刷新进度从原采集库挪来，从新周期开始）；V046 只改名：表 `history_*` → `fabric_*`；V047 加 `fetch_enabled` 和改开关的记录，并关掉 `fetch/catalog.py · DEFAULT_CLOSED` 里来源的可读开关。

测试：`make test`（单元，不连库）、`make lint`、`make metrics`（函数 80 行、复杂度 20 的门槛）、`make test-integration`（起临时
PostgreSQL 容器，跑完删掉）、`make check-lock`（要连 PyPI；服务器上临时加 `HTTPS_PROXY=http://127.0.0.1:7890`）。

## 读代码的顺序

1. `main_etl.py`：整条链一页读完，每一步调用的是哪个文件的哪个函数。
2. 抓：`fetch/catalog.py`（有哪些来源）→ `fetch/fetch_job.py` 的 `run_fetch_job` → `fetch/sources.py` 的
   `read_public` → 网站走 `fetch/site_crawler.py`，站点知识在 `fetch/sites/<站点>.py` 和 `fetch/extractors/`。
3. 存版本：`store/records.py` 的 `finish_fetch` → `_publish`（投影 `fetch/projection.py`，上面“版本怎么定”）。
4. 洗：`clean/clean_stage.py` 的 `process` → `clean_text.py`、`identifiers.py`、`profile.py`；
   送知识库 `store/index_publication.py`；对外 `api/routers/fabric.py`。

经过哪些文件、函数和表见 [docs/代码地图.md](docs/代码地图.md)（学生不用读 `tests/`）。出网只走 `fetch/transport.py` 的 `get`（限速、超时、429 重试），不要自己 `sleep`。

## 怎么加一个站点

1. `fetch/sites/` 加一个文件，写 `SITE_KEY` 和 `CONFIG`（列表入口 `start_urls`、选择器、翻页；接口站点写 `"source_type": "api"`
   和 `api_fetcher`），照最像的站点抄（网页看 `nmpa.py`，接口看 `fda/`）；通用解析读不好详情页时在 `fetch/extractors/` 写提取函数。
2. `fetch/catalog.py` 的 `SPECS` 加一行 `_site(...)`：编号、名称、证据类型、业务判断、限速组和间隔、覆盖说明（有附件写
   `attachments`）；列表不是从新到旧的写 `incremental=_FULL_REREAD`。临时库上跑一次迁移给 `fabric_sources` 补一行。
3. `make test`；真抓 `python main_etl.py --source <站点> --max 5`，再 `python ops/check_sources.py <站点> --rounds 3 --incremental`
   （第二、三轮应停在 reached_known）；照 `tests/test_site_pages.py` 存一页夹具，补一个提取测试；要定时刷新的写进 WATCHES。

## 现状

Python 3.12。图谱（抽取、消歧、审核、图发布）已移到 Atlas；测量子系统已删除（V048 删表）；WeKnora 桥接服务在本仓 `weknora/`，WeKnora 本身用官方镜像，由部署仓库 a54b19211/ZK-radar-deploy 部署；查询规划在 a54b19211/Bio-Planner。

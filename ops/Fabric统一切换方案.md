# Fabric 统一实现：现役切换方案（待复核，未执行）

适用分支：feat/collection-chain-a（T-28 收口后的固定提交）。现役当前是 Fabric 04a8888 一系（cd027e7 热修）。
本文只写方案和核对口径；**任何一步上现役都要等主持人在对话里明确同意。**

## 切换前后的差别（与现役有关的部分）

| 项 | 现役 | 切换后 |
|---|---|---|
| documents → history 冻结 | publisher 自动冻结（outbox + 触发器 + backfill） | 不变；新增：history 固定版本作为抽取主体的行不再被冻结回 history |
| 采集入口 | 框架（CT/EPMC/WHO 窄 watch）＋旧 ETL 代码在库里但没在跑 | 只有框架；旧 ETL、旧上传/重抓接口删除 |
| 进 WeKnora | index_publication（另有两条旧路径的代码，没在跑） | 只有 index_publication |
| documents 表结构 | — | V026：加 history_* 引用列（全部可空），替换触发器函数 |
| history 库 | — | V022–V025 派生表（只追加，新增，不改已有表） |
| 索引作业表 | — | V027：index_publications 加两个只读排序索引（普通 CREATE INDEX，现役约 9.5 万行，持锁很短） |
| 读取预算 | 各读取器各自的字节上限变量 | 只有 DATAFABRIC_READ_MAX_BYTES（默认 256MiB/次读取）；DATAFABRIC_SITE_RUN_MAX_BYTES、DATAFABRIC_FULLTEXT_RUN_MAX_BYTES、DATAFABRIC_LOCAL_IMPORT_RUN_MAX_BYTES 不再读取，部署环境里有就删掉 |

## 步骤

### 0. 前置
- Codex 对 T-28 固定提交复核通过；主持人明确同意上线。
- 备份：documents 库 `pg_dump --schema-only` + 触发器函数定义；history 库派生表为新增，回退不需要数据备份。

### 1. 停写确认（只读）
- 进程清单：现役没有 documents 内容的写入者（旧 ETL、上传/重抓接口均未运行；抽取 worker 只改状态列，审核只改实体表，不触发文档冻结）。
- 记下高水位 `HW = max(documents.id)` 和记录时间。之后若出现 id > HW 的新行或 HW 以内内容变化，说明还有写入者，停止切换。

### 2. 最终追平（只读核对）
- 现役 publisher 继续跑到 backfill 完成。
- 运行对账：`python -m flows.scheduler --reconcile <HW>`（只读、按 id 分批、单个快照）。
- 通过条件：`switchable=true`，即 HW 以内每一行 `matched`（当前内容指纹 = 最后一次成功发表的指纹），unpublished / stale / in_flight 都是 0，outbox 没有未完成的 document/graph 待办。
- 不用“某时刻 pending=0”当依据：还有写入者时它会骗人。

### 3. 切入口
1. 停现役 Fabric 三个服务（API、publisher、抽取 worker），按已核实 PID。
2. documents 库：`python -m migration.runner --db publication --apply`（V019 原样 + V026：加列、替换触发器函数）。
3. history 库：`migration.history`（V017…V025，全部 IF NOT EXISTS / 新增表）。
   索引作业表：同一条 history 迁移已装（V020、V024 原样 + V027 两个索引）。
4. 许可目录：已存在的来源行说明文字不会被 `ON CONFLICT DO NOTHING` 更新，另跑一条 UPDATE 写入新的站点来源说明（nmpa/cde/chictr/cde_xxgk/fda_api_* 现在是 site_plugin 来源兼库存许可）。
5. 部署分支固定提交，启动三个服务；allowlist 按现役原值，**新增来源（local_files、新站点）默认不放开**。

### 4. 核验
- 再跑一次 `--reconcile <HW>`：仍全部 matched（证明切换没有改写历史身份）。
- `GET /api/history/source-health`、`/index/counts`、`/index/status`：现役窄 watch 照常推进，索引状态正常。
- 清洗段：`python -m flows.clean_stage --status`，看各来源水位与死信。
- 抽取：对一个 CT 固定版本调 `POST /api/extraction/history-version`，确认主体登记、读取的是该版本清洗正文（不需要真实大批量）。
- 雷达（若同时切换）：按雷达自己的核验清单。

### 5. 恢复（任何一项核验失败）
1. 停新服务。
2. 回到现役提交（04a8888 / cd027e7）启动。V026 加的列全部可空，旧代码不读也不写它们；history 派生表是新增表，旧代码不读。
3. 触发器函数恢复为 V019 原版（`migration/versions/V019__publication_outbox.sql` 里的 `publication_document_changed` 定义，单独执行该函数定义即可）。
4. 再跑 `--reconcile <HW>` 确认 documents 侧没有变化。

## 未在本方案内的
- 抽取、审核、消歧仍以 documents 行为主体；history 固定版本可以作为主体进入同一条链（T-28 ⑤），documents 库存继续经冻结桥进 history。是否把 documents 库存的抽取也改为读它的 legacy_documents 固定版本，是 3.4/3.5 的事。
- 站点插件（18 个）都是 verified=False，切换后默认不放开采集；openFDA 两个接口已做真实小批实测（见 T-28 汇报）。

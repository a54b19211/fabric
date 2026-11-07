# ZK Data Fabric 审核后台 API

FastAPI 后端，提供文档查询、LLM 抽取触发、审核操作、图数据推送到 WeKnora 的 Neo4j 拷贝库，以及正文索引发布（正文进 WeKnora 的唯一路径）的管理接口。

## 启动

```bash
# 安装依赖（如果 requirements.txt 里没有 fastapi，需要补上）
pip install fastapi uvicorn

# 启动开发服务器（自动重载）
python api/main.py

# 或者用 uvicorn 命令
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 接口列表

### 文档模块
- `GET /api/documents/sites` - 文档库里实际出现的来源站点
- `GET /api/documents` - 文档列表（分页 + 筛选）
- `GET /api/documents/{uuid}` - 文档详情

文档只读；重爬、上传 MD 的接口已删除，新资料只经采集框架（`collection/`）入 History。

### LLM 抽取模块
- `POST /api/extraction/trigger` - 触发抽取
- `GET /api/extraction/stats` - 抽取状态统计
- `GET /api/extraction/status/{uuid}` - 单篇文档抽取状态

### 审核模块
- `GET /api/review/entities` - 待审核实体列表
- `GET /api/review/entities/{id}` - 实体详情
- `POST /api/review/entities/{id}/approve` - 审核通过
- `POST /api/review/entities/{id}/reject` - 审核拒绝
- `GET /api/review/relations` - 待审核关系列表
- `POST /api/review/relations/{id}/approve` - 审核通过
- `POST /api/review/relations/{id}/reject` - 审核拒绝

### WeKnora 图数据推送模块
- `POST /api/weknora/graph/sync-entity-dict` - 同步实体字典
- `POST /api/weknora/graph/init-copies` - 初始化 PrimeKG 副本
- `POST /api/weknora/graph/sync-entities` - 同步图实体
- `POST /api/weknora/graph/sync-relations` - 同步图关系
- `POST /api/weknora/graph/sync-all` - 依次执行以上推送
- `GET /api/weknora/graph/stats` - 图同步状态统计

这里只推图数据。正文（文档原文/清洗正文）进 WeKnora 只有一条路径：`db/index_publication.py` + 桥接服务，
管理接口见下文“正文索引发布”。旧的文档同步接口（`/api/weknora` 下的待同步列表、单篇同步、批量同步、文档同步统计）已删除，不要再调用。

### 正文索引发布（`/api/history/index`，正文进 WeKnora 的唯一路径）

后台由发表循环里的 `index_tick` 自动处理：扫描已提交原件 → 建 `index_publications` 作业 → 经桥接服务发送并核对回执。
以下接口都要 `Authorization: Bearer <DATAFABRIC_HISTORY_ADMIN_TOKEN>`；缺凭据 401，不是管理员凭据 403。
前端入口：导航“同步 → 正文索引发布”（`/index-publications`），页面就是调用这三个接口。

| 接口 | 用途 |
| --- | --- |
| `GET /api/history/index/counts` | 各状态作业数：`{"states": {...}, "total": n}`，没出现的状态记 0 |
| `GET /api/history/index/status` | 作业列表，按更新时间倒序。参数：`limit`（1–100，默认100）、`state`、`source_id`、`cursor`；返回 `{"items": [...], "next_cursor": ...}`，`next_cursor` 不为空就把它原样作为 `cursor` 取下一页 |
| `POST /api/history/index/{id}/retry` | 把受阻（`blocked`）作业放回待重试；`id` 是 Fabric 作业编号（64 位十六进制），不是 WeKnora 知识条目编号 |

作业状态：`queued` 待发送、`sending` 发送中、`indexing` 桥接服务解析/向量化中、`ready` 已就绪、
`waiting` 失败后自动退避重试、`blocked` 连续失败 6 次不再自动重发、`disabling` 停用中、`disabled` 已停用（来源撤权、出了新版本或原件撤回）。
每条作业带 `error`（`{code, message}`，最近一次失败原因）、`attempts`、`remote_id`、`version_id`、`artifact_key`（附件正文产物）、`source`（索引目标配置）、`read_permitted`（来源当前是否仍有许可）。

失败重试：`waiting` 不用管，会自动退避重试；`blocked` 要先按 `error.code` 修好原因，再调 retry（或在页面点“重试”）。
retry 返回 `{"scheduled": true, "state": "waiting"}` 表示已放回队列，下一轮 `index_tick` 处理，处理时仍先向桥接服务核对已发请求，不会重复上传；
`{"scheduled": false, "reason": "not_blocked"}` 表示作业不是受阻状态、没有改动；`reason: "leased"` 表示正被处理，稍后再试；作业编号不存在 404，格式不对 422。
参数错误（状态值、续页标记不对）返回 422；数据库不可用返回 503 `history_service_failed`。

## 依赖

- FastAPI
- Uvicorn
- Pydantic
- SQLAlchemy（已有）
- Loguru（已有）

## TODO

- [ ] 接入成员3的 `extraction_flow`（LLM 抽取）
- [x] 正文进 WeKnora 只走 db/index_publication（经桥接服务）


## 雷达使用图数据

雷达先选资料和对象，再通过这里读取图。图关系可以提示下一步查什么；它本身不等于药物有效、安全或机制已经证实。

### 先用名称取得真实编号

第一次只知道 EGFR 这样的名称时，调用 `POST /api/history/graph/primekg/lookup`，使用与 capture 相同的独立图查询凭据。例如：

```json
{"name": "EGFR", "node_type": "gene/protein", "limit": 5}
```

返回的 `candidates` 含图里实际的 `name`、`node_type`、`node_source`、`seed_id`；选定后把后三项原样传给 capture。名称按同类型精确匹配，不区分大小写；最多返回 20 项。只有一个候选时 `status=resolved`，多个为 `ambiguous`，没有为 `not_found`。即使 limit=1，只要还有其他候选，也会报告歧义和截断，不能自动采用第一条。

该入口只查当前名称，不做别名推断或身份审核，不创建 History 版本。BCMA 等简称若未录为 PrimeKG 节点名称，必须明确未找到；有原资料或已核查字典中的基因符号时，可以再据此查询，不能让模型补一个编号。原有审核页的 `dict_id` 是本地字典编号，不能冒充 PrimeKG 的原生编号。

候选的 `observed_at` 是本次查询时间。历史调查应直接使用当时的固定图版本及 resolve；当前名称候选不能证明过去已知身份，更不能绕过 capture 版本的首次收到时间。请求拒绝 cutoff、任意查询语句和外部地址。

### 查询当前 PrimeKG

`POST /api/history/graph/primekg/capture` 使用独立的 `DATAFABRIC_GRAPH_QUERY_TOKEN`，放在 `Authorization: Bearer ...`。另需 `Idempotency-Key`，长度为 1–160 个可见 ASCII 字符。例如请求体：

```json
{
  "node_type": "gene/protein",
  "node_source": "NCBI",
  "seed_id": "1956",
  "relation_types": [],
  "neighbor_term": "lung",
  "limit": 10
}
```

这是从 EGFR 出发、查名字含 lung 的邻居。类型只能是 drug、disease、gene/protein；来源和 ID 必须精确。关系类型列表最多 16 项，limit 为 1–200。查询只读一跳，使用固定参数化语句。接口不接收 Cypher、外部地址或历史截止时间。

成功返回 `run`、`query`、`read_cutoff` 和 `neighborhood`：

- `run` 是实际完成发布的任务，不是“已发起”。
- `neighborhood.graph_ref` 给出来源、原生记录 ID、固定版本、原件摘要及本地首次收到时间。
- `nodes` 和 `edges` 保留真实方向及背景标记。没有原文证据绑定，不能转成已验证机制证据。
- `seeds_missing` 表示本次没有找到指定身份；找到身份但没有边则返回该节点和空边。
- `coverage` 说明截断原因。单次返回最多 100 个节点、200 条边，未返回的部分不能当作不存在。

同一查询键和同一参数重试，会读取第一次采用的固定版本，不重新采集。关系类型的顺序和重复项不影响同一次查询。同一键换参数返回 409。要重新观察今天的图，使用新的查询键。并发或响应丢失后也按原键重试；不要猜测是否成功后直接换键。

失败不会冒充空结果：缺查询凭据配置为 `graph_query_not_configured`（503），缺 Neo4j 配置为 `primekg_not_configured`（503），读取超时为 `primekg_timeout`（504），未形成版本为 `graph_not_published`（409）。其他来源许可、HTTP 和协议错误也保留明确错误码。服务端不自动重试本次捕获，调用方须限制重试次数并退避。

### 使用已经保存的版本

后续调用沿用 History 的读凭据 `DATAFABRIC_HISTORY_READ_TOKEN`。不要把图查询凭据当通用归档或管理员凭据。

- `GET /api/history/graph/{version_id}/resolve`：按精确名称，或外部命名空间与 ID 找对象。
- `GET /api/history/graph/{version_id}/neighborhood`：指定 `seed_ids=primekg.NCBI|1956` 等明确身份，读取固定包内邻域。
- `GET /api/history/versions/{version_id}` 及其 `/raw`：查看结构化版本和原件。
- 公共参数：`source_id`、`source_record_id`、`is_demo`、`mode`、`cutoff`。当前查询可以直接沿用响应中的 `graph_ref` 和 `read_cutoff`。

历史调查必须使用原定的 cutoff，只读当时已发布的版本。**今天捕获的图不能放进过去的调查，也不能为了让读取成功而前移 cutoff。** 没有当时的图就说明缺资料。PrimeKG 当前捕获不支持 `public_history_reconstruction`；不能凭知识库里有一条关系就声称过去已经知道。普通图中 `resolved` 也只表示唯一匹配，仍需查看身份、审核和原文绑定状态。

### 运行配置与部署

沿用 History 数据库和来源许可，许可必须包含 `primekg_graph`。新增或确认以下私有配置：

| 配置 | 用途 |
| --- | --- |
| DATAFABRIC_GRAPH_QUERY_TOKEN | 仅授权当前图受限查询和结果发布；不同于读/管理员凭据 |
| DATAFABRIC_GRAPH_NEO4J_URL | 同机 Neo4j HTTP 根地址；仅允许 loopback 地址 |
| DATAFABRIC_GRAPH_NEO4J_USER / PASSWORD | 图访问凭据，不能写入仓库、示例或交接报告 |
| DATAFABRIC_GRAPH_NEO4J_DATABASE | 可选，默认 neo4j |

本接口无需新表、不改已有变化流。查询包采用后进入 `changes`，但 PrimeKG 的“新变化”只表示新收到一次当前背景观察。雷达不能把它当成真实世界刚出现的新机制。文档入库后的 outbox 发布链继续使用原服务；本接口不启动采集、审核或图同步。

本轮代码先在独立分支及临时 History 库验证。是否更新现役代码、补配置和重启服务，由部署负责人统一安排，不能把测试地址写死到产品配置。

## 固定版本图查询与索引状态

以下路径位于 `/api/history` 下，沿用独立身份验证，不改变原 documents、research 接口。

| 接口 | 用途 | 身份要求 |
| --- | --- | --- |
| `GET /graph/{version_id}/paths` | 固定图包内查路径，最多 4 跳，限制路径数和展开数 | History 读取凭据 |
| `GET /graph/{version_id}/subgraph` | 返回指定对象间的有界子图 | History 读取凭据 |
| `POST /graph/native/capture` | 将 1 至 20 个已采用 CT 固定版本投成登记字段图 | History 管理凭据 |
| `GET /index/counts` | 正文索引作业各状态计数 | History 管理凭据 |
| `GET /index/status` | 查看任务与上传、解析、向量、绑定状态；可按 `state`、`source_id` 过滤，`cursor` 续页 | History 管理凭据 |
| `POST /index/{id}/retry` | 恢复被阻断任务，仍先核对已发请求 | History 管理凭据 |

图查询须给 `source_id`、`source_record_id`、`is_demo`、`mode`、`cutoff`，对象为 `namespace|id`。新路径和子图接口默认 `review_scope=reviewed`；`with_candidates` 才含待审或未绑定候选，仍保留原状态。原邻居和解析接口增加同名可选参数，旧调用兼容，新业务应显式选择范围。

索引目标按来源实例、来源编号、演示标志、知识库和业务范围固定。四源示例只代表明确配置的四个来源；`legacy_documents` 已退役，不能作索引目标（配置了它，服务和索引 worker 启动时拒绝：`index_target_retired_source`）；不能把不同用途资料悄悄混装。配置删除后仍继续清理旧绑定，停止进程不等于清理完成。

原文读取仍走 `/versions/{version_id}` 与 `/versions/{version_id}/raw`。固定版本与当下许可必须同时满足，索引命中不能代替原件权限检查。

WHO v0.2 的 `count_statements` 与 `count_extraction` 是派生 facts，不能走逐字段顶层 JSON literal 校验。应核对 `who.explicit-count.v1`、原始字段范围和完整 `clean_text` 的 Unicode 字符位置，逐字核对 quote 与数值；完整契约见本轮交接的 `fabric-who-count-contract.json`。旧 v0.1 没有这两个字段，仍按原契约读取。

### 测量对象（`/api/history/measurements`，加工层，docs/decisions/0006）

| 接口 | 用途 |
|---|---|
| `GET /api/history/measurements?source_id=&is_demo=&mode=&cutoff=&version_ids=` | 可见版本的已核验测量对象（`datafabric.data_objects.v2`），每个带截止时的状态（由事件折叠）、身份、维度核验结果和所用版本；默认只给该模式下当前的，`include_history=true` 连历史一起 |
| `GET /api/history/measurements/{measurement_id}?is_demo=&mode=&cutoff=` | 一个固定对象（含已替代、非当前的）；截止时不可见返回 404 `version_not_found` |
| `GET /api/history/measurements/changes?is_demo=&after_seq=&limit=` | 事件变更流，按提交顺序；`next_after` 是看过的最后一个序号；当前不可读来源的事件只计入 `withheld` |
| `GET /api/history/measure-coverage?source_id=&is_demo=&mode=&cutoff=&version_ids=` | 每个版本的阅读 run：状态、已读窗口、欠着的窗口及原因（`partial` 是覆盖缺口，不等于没有数字） |
| `POST /api/history/measurements/{measurement_id}/identities`（管理员） | 把对象绑定到固定图包里已确认、且证据就是这处提及的身份；否则 409 |

消费方（雷达 `series_sync`）的约定见 `tests/fixtures/measurement_feed_contract.json`。

# ZK Data Fabric 审核后台 API

FastAPI 后端，提供文档查询、LLM 抽取触发、审核操作、WeKnora 同步（文档 + 图数据）等接口。

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
- `GET /api/documents` - 文档列表（分页 + 筛选）
- `GET /api/documents/{uuid}` - 文档详情

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

### WeKnora 同步模块
- `GET /api/weknora/pending` - 待同步文档列表
- `POST /api/weknora/sync/{uuid}` - 单篇同步
- `POST /api/weknora/sync-batch` - 批量同步文档
- `POST /api/weknora/graph/sync-entity-dict` - 同步实体字典
- `POST /api/weknora/graph/init-copies` - 初始化 PrimeKG 副本
- `POST /api/weknora/graph/sync-entities` - 同步图实体
- `POST /api/weknora/graph/sync-relations` - 同步图关系
- `GET /api/weknora/graph/stats` - 图同步状态统计
- `GET /api/weknora/stats` - 文档同步状态统计

## 依赖

- FastAPI
- Uvicorn
- Pydantic
- SQLAlchemy（已有）
- Loguru（已有）

## TODO

- [ ] 接入成员3的 `extraction_flow`（LLM 抽取）
- [ ] 接入 `sync_to_weknora.py` 的同步逻辑


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
| `GET /index/status` | 查看任务与上传、解析、向量、绑定状态 | History 管理凭据 |
| `POST /index/{id}/retry` | 恢复被阻断任务，仍先核对已发请求 | History 管理凭据 |

图查询须给 `source_id`、`source_record_id`、`is_demo`、`mode`、`cutoff`，对象为 `namespace|id`。新路径和子图接口默认 `review_scope=reviewed`；`with_candidates` 才含待审或未绑定候选，仍保留原状态。原邻居和解析接口增加同名可选参数，旧调用兼容，新业务应显式选择范围。

索引目标按来源实例、来源编号、演示标志、知识库和业务范围固定。四源示例只代表明确配置的四个来源；既有 `legacy_documents` 应按实际授权和业务范围配置，不能把不同用途资料悄悄混装。配置删除后仍继续清理旧绑定，停止进程不等于清理完成。

原文读取仍走 `/versions/{version_id}` 与 `/versions/{version_id}/raw`。固定版本与当下许可必须同时满足，索引命中不能代替原件权限检查。

WHO v0.2 的 `count_statements` 与 `count_extraction` 是派生 facts，不能走逐字段顶层 JSON literal 校验。应核对 `who.explicit-count.v1`、原始字段范围和完整 `clean_text` 的 Unicode 字符位置，逐字核对 quote 与数值；完整契约见本轮交接的 `fabric-who-count-contract.json`。旧 v0.1 没有这两个字段，仍按原契约读取。

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

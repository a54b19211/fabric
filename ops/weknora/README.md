# WeKnora 部署配置

WeKnora 本身用官方镜像运行（`wechatopenai/weknora-app`、`weknora-ui`、`weknora-docreader`、`weknora-sandbox`，
版本由 `.env` 的 `WEKNORA_VERSION` 定，默认 `latest`），不再维护 WeKnora 源码分叉。Fabric 自己的扩展只有
`weknora/` 桥接服务（见 `docs/weknora桥接.md`），它经 WeKnora 的 HTTP API 写、只读连 WeKnora 的 PostgreSQL。

- `docker-compose.yml`：官方仓库的 compose 原样拷贝（来自服务器上 /data/projects/WeKnora 检出）。
- `docker-compose.override.yml`：本机改动——主机 PG17 占了 5432，WeKnora 库只发布在 `127.0.0.1:15432`。
- `config/config.yaml`：WeKnora 的应用配置（不含密钥）。
- `.env` 不在仓库：口令、模型密钥等由运维放在部署目录，按官方 `.env.example` 填。

用法（部署目录里放这三个文件加 `.env`）：`docker compose up -d`；compose 按服务名 profile 启用可选组件，见官方 README。

app 还挂载 `./skills/preloaded`（可选，缺了 docker 建空目录）；searxng（`./docker/searxng/settings.yml`）和 dex
（`./misc/dex-config.yaml`）是可选 profile，用到时从官方仓库取对应文件。

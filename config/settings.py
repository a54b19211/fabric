"""
全局配置中心 —— 使用 pydantic-settings 从 .env 文件 / 环境变量读取配置。
所有 API Key、路径、阈值统一在此管理，其他模块只需 `from config.settings import APP`。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class _AppSettings(BaseSettings):
    """
    只需在项目根目录放一个 .env 文件（参考 .env.example），
    pydantic-settings 会自动读取，也支持直接设置系统环境变量覆盖。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",            # 忽略 .env 中多余的变量，不报错
    )

    # ======================== 云端 API 密钥 ========================
    MINERU_API_BASE: str = "http://100.121.201.73:8000"

    # ── LLM（OpenAI 兼容协议，支持 Qwen / DeepSeek / GPT / 本地 Ollama 等）──
    # 实体抽取阶段使用（图谱节点/关系抽取）
    # 爬虫阶段不调用 LLM，只用 Trafilatura + MinerU
    LLM_API_KEY: str = ""
    LLM_API_BASE: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL_NAME: str = "qwen-plus"

    # 实体抽取专用模型（可选，留空则复用 LLM_* 配置）
    EXTRACTION_LLM_API_KEY: str = ""
    EXTRACTION_LLM_API_BASE: str = ""
    EXTRACTION_LLM_MODEL_NAME: str = ""

    # 消歧精排专用模型（可选，留空则复用 LLM_* 配置）
    RERANK_LLM_API_KEY: str = ""
    RERANK_LLM_API_BASE: str = ""
    RERANK_LLM_MODEL_NAME: str = ""

    # ── Embedding（DashScope text-embedding-v3，消歧向量检索用）──
    EMBEDDING_API_KEY: str = ""          # 留空则复用 LLM_API_KEY
    EMBEDDING_API_BASE: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBEDDING_MODEL_NAME: str = "text-embedding-v3"
    EMBEDDING_DIMENSIONS: int = 1024     # text-embedding-v3 输出维度

    @property
    def EMBEDDING_KEY(self) -> str:
        return self.EMBEDDING_API_KEY or self.LLM_API_KEY

    @property
    def RERANK_API_KEY(self) -> str:
        return self.RERANK_LLM_API_KEY or self.LLM_API_KEY

    @property
    def RERANK_API_BASE(self) -> str:
        return self.RERANK_LLM_API_BASE or self.LLM_API_BASE

    @property
    def RERANK_MODEL_NAME(self) -> str:
        return self.RERANK_LLM_MODEL_NAME or self.LLM_MODEL_NAME

    # 兼容旧变量名（已有代码用 QWEN_* 的不需要改）
    @property
    def QWEN_API_KEY(self) -> str:
        return self.LLM_API_KEY

    @property
    def QWEN_API_BASE(self) -> str:
        return self.LLM_API_BASE

    @property
    def QWEN_MODEL_NAME(self) -> str:
        return self.LLM_MODEL_NAME

    @property
    def EXTRACTION_API_KEY(self) -> str:
        return self.EXTRACTION_LLM_API_KEY or self.LLM_API_KEY

    @property
    def EXTRACTION_API_BASE(self) -> str:
        return self.EXTRACTION_LLM_API_BASE or self.LLM_API_BASE

    @property
    def EXTRACTION_MODEL_NAME(self) -> str:
        return self.EXTRACTION_LLM_MODEL_NAME or self.LLM_MODEL_NAME

    # ======================== 本地数据根目录 ========================
    DATA_ROOT: Path = Path("data")

    @property
    def RAW_DIR(self) -> Path:
        """原始文件存放目录"""
        return self.DATA_ROOT / "raw"

    @property
    def MARKDOWN_DIR(self) -> Path:
        """Markdown 全文存放目录"""
        return self.DATA_ROOT / "markdown"

    @property
    def JSON_DIR(self) -> Path:
        """JSON 元数据信封存放目录"""
        return self.DATA_ROOT / "json_envelopes"

    @property
    def LINEAGE_DIR(self) -> Path:
        """字段级血缘追踪记录目录"""
        return self.DATA_ROOT / "lineage"

    @property
    def QUARANTINE_DIR(self) -> Path:
        """隔离区——质量不达标的数据暂存"""
        return self.DATA_ROOT / "quarantine"

    @property
    def REVIEW_DIR(self) -> Path:
        """人工审核 CSV 导出目录"""
        return self.DATA_ROOT / "review"

    # ======================== 质量门控阈值 ========================
    MIN_MARKDOWN_LENGTH: int = 100          # Markdown 字符数低于此值判定为解析失败


    # ======================== 并发与重试 ========================
    MAX_CONCURRENCY: int = 3                # Prefect 并发任务数，防封 IP
    TASK_RETRIES: int = 3
    TASK_RETRY_DELAY: int = 10              # 秒
    EXTRACTION_CONCURRENCY: int = 3         # LLM 抽取 Worker 数量（并行文档数）

    # ======================== 任务队列 ========================
    TASK_QUEUE_BACKEND: str = "memory"      # "memory" | "redis" — 任务队列后端
    REDIS_URL: str = "redis://localhost:6379/0"  # Redis 连接地址（backend=redis 时使用）

    # ======================== 远程数据库 ========================
    # 支持 PostgreSQL / MySQL，留空则不推送
    DB_HOST: str = ""
    DB_PORT: int = 5432
    DB_NAME: str = ""
    DB_USER: str = ""
    DB_PASSWORD: str = ""
    # 连接池大小（需容纳前端查询 + 抽取 worker 并发）
    DB_POOL_SIZE: int = 20

    @property
    def DB_URL(self) -> str:
        """SQLAlchemy 连接串，未配置时返回空字符串。"""
        if not self.DB_HOST or not self.DB_NAME:
            return ""
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    # ======================== 本地存储开关 ========================
    # 开发/调试时可启用本地文件存储，生产环境建议关闭（只保留数据库）
    ENABLE_LOCAL_STORAGE: bool = False

    # ======================== WeKnora 同步配置 ========================
    WEKNORA_BASE_URL: str = "http://localhost:8080"
    WEKNORA_API_KEY: str = ""
    WEKNORA_KB_ID: str = ""

    # ======================== MinIO 船票预留（二期使用）========================
    FUTURE_MINIO_BUCKET: str = "zk-data-fabric"


# 全局单例——整个项目统一用这个
APP = _AppSettings()

